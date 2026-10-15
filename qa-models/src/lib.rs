//! Bounded, copied MODEL of the logical waiter/epoch publication protocol.
//! This crate does not claim to execute GlobalBudget or its physical fit scan.

#[cfg(test)]
mod tests {
    use loom::sync::atomic::{AtomicUsize, Ordering};
    use loom::sync::{Arc, Mutex};
    use loom::thread;

    struct Publication {
        // Each lock acquisition models ONE SeqCst operation. Loom 0.7.2
        // weakens SeqCst atomics to AcqRel, so a direct substitution can
        // report executions forbidden by the production SC contract.
        sc: Mutex<(usize, bool)>,
        slot: Mutex<bool>,
        ready: AtomicUsize,
    }

    impl Publication {
        fn epoch(&self) -> usize {
            self.sc.lock().unwrap().0
        }
        fn waiting(&self) -> bool {
            self.sc.lock().unwrap().1
        }
        fn publish(&self, value: bool) {
            self.sc.lock().unwrap().1 = value;
        }
        fn release(&self) {
            self.sc.lock().unwrap().0 += 1;
        }
    }

    fn check(publish_first: bool) {
        let executions = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let counted = executions.clone();
        let mut model = loom::model::Builder::new();
        model.preemption_bound = Some(2);
        model.max_branches = 1000;
        model.max_permutations = Some(100_000);
        model.check(move || {
            counted.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            let state = Arc::new(Publication { sc: Mutex::new((0, false)), slot: Mutex::new(false), ready: AtomicUsize::new(0) });
            let waiter = state.clone();
            let register = thread::spawn(move || {
                // Mutation reproduces the wrong ordering: a release may see
                // no waiter after this check but before publication.
                let before = if publish_first { 0 } else { waiter.epoch() };
                let mut slot = waiter.slot.lock().unwrap();
                *slot = true;
                waiter.publish(true);
                let observed = if publish_first { waiter.epoch() } else { before };
                if observed != 0 {
                    *slot = false;
                    waiter.publish(false);
                    drop(slot);
                    waiter.ready.fetch_add(1, Ordering::SeqCst);
                }
            });
            let releaser = state.clone();
            let release = thread::spawn(move || {
                releaser.release();
                if releaser.waiting() {
                    let mut slot = releaser.slot.lock().unwrap();
                    let wake = std::mem::take(&mut *slot);
                    releaser.publish(false);
                    drop(slot);
                    if wake {
                        releaser.ready.fetch_add(1, Ordering::SeqCst);
                    }
                }
            });
            register.join().unwrap();
            release.join().unwrap();
            assert_eq!(state.ready.load(Ordering::SeqCst), 1, "lost unique release");
            assert!(!state.waiting());
            assert!(!*state.slot.lock().unwrap());
        });
        eprintln!(
            "model=logical-waiter-publication preemption_bound=2 max_permutations=100000 executions={}",
            executions.load(std::sync::atomic::Ordering::Relaxed)
        );
    }

    #[test]
    fn publish_then_check_preserves_only_release() {
        check(true);
    }

    #[test]
    #[should_panic(expected = "lost unique release")]
    fn negative_control_check_then_publish_loses_release() {
        check(false);
    }
}
