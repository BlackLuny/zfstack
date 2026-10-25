//! Real Tokio driver in the existing FIFO/link fixture. The channel bridge is
//! fixture-only; its total CPU and the enclosing process CPU are reported.
use super::{stack_cfg, ZfOpts};
use crate::app::{AppConn, CloseAction, ServerCounters};
use crate::isolate::{self, IsolatePlan};
use crate::stack::{Batch, StackLive, StackResult, UserStack};
use crate::{tun, util};
use crossbeam_channel::{Receiver, Sender};
use std::io;
use std::sync::atomic::{AtomicBool, Ordering::Relaxed};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use zfstack::budget::GlobalBudget;
use zfstack::tokio_adapter::{self, ResourceLimits, StreamConfig};
use zfstack::{IfaceConfig, IfaceId, OutPacket, PeerId, SendResult};

pub struct TokioStack {
    opts: ZfOpts,
    counters: Arc<ServerCounters>,
    plan: Option<IsolatePlan>,
    splice: bool,
}

async fn app(mut stream: tokio_adapter::TcpStream, counters: Arc<ServerCounters>) -> io::Result<()> {
    let mut app = AppConn::new(counters);
    let mut read_buf = vec![0; 65536];
    let mut write_buf = vec![0; 65536];
    loop {
        if app.wants_send() {
            let n = app.produce(&mut write_buf);
            stream.write_all(&write_buf[..n]).await?;
            continue;
        }
        match app.close_action() {
            CloseAction::Close => return stream.shutdown().await,
            CloseAction::Abort => return Err(io::Error::new(io::ErrorKind::Unsupported, "abort command is not a Tokio fixture case")),
            CloseAction::None => {}
        }
        let n = stream.read(&mut read_buf).await?;
        if n == 0 {
            app.on_peer_eof();
        } else {
            app.on_recv(&read_buf[..n]);
        }
    }
}

impl TokioStack {
    pub fn new(opts: ZfOpts, counters: Arc<ServerCounters>, plan: Option<IsolatePlan>, splice: bool) -> Self {
        Self { opts, counters, plan, splice }
    }
}

impl UserStack for TokioStack {
    fn ingress(&mut self, _: Instant, _: &[u8]) {
        unreachable!("TokioStack uses its native driver")
    }
    fn poll(&mut self, _: Instant, _: &mut dyn FnMut(&[u8])) {
        unreachable!("TokioStack uses its native driver")
    }
    fn next_deadline(&mut self, _: Instant) -> Option<Instant> {
        unreachable!("TokioStack uses its native driver")
    }

    fn drive(self: Box<Self>, rx: Receiver<Batch>, tx: Sender<Batch>, stop: Arc<AtomicBool>, live: Arc<StackLive>) -> StackResult {
        util::set_timerslack_ns(1);
        let cpu0 = util::thread_cpu_now();
        let upstream_stop = Arc::new(AtomicBool::new(false));
        let upstream = self.splice.then(|| crate::kernel_server::start_at(
            self.counters.clone(), upstream_stop.clone(), None, None, "127.0.0.1:5201".parse().unwrap(),
        ).expect("kernel upstream server"));
        let global_bytes = self.plan.as_ref().map_or(512 << 20, |p| if p.budget_mb == 0 { 512 << 20 } else { p.budget_mb << 20 });
        let port_bytes = self.plan.as_ref().map_or(64 << 20, |p| if p.budget_mb == 0 { 64 << 20 } else { global_bytes / 2 });
        let peer_bytes = self.plan.as_ref().map_or(32 << 20, |p| if p.budget_mb == 0 { 32 << 20 } else { global_bytes / 2 });
        let global = GlobalBudget::new(global_bytes);
        let rt = tokio::runtime::Builder::new_current_thread().enable_all().max_blocking_threads(1).build().unwrap();
        let mut stats = rt.block_on(async {
            let (input_tx, input_rx) = tokio::sync::mpsc::channel::<Batch>(256);
            let bridge_stop = stop.clone();
            let bridge = tokio::task::spawn_blocking(move || {
                let start = util::thread_cpu_now();
                while !bridge_stop.load(Relaxed) {
                    match rx.recv_timeout(Duration::from_millis(50)) {
                        Ok(batch) => if input_tx.blocking_send(batch).is_err() { break },
                        Err(crossbeam_channel::RecvTimeoutError::Timeout) => {}
                        Err(crossbeam_channel::RecvTimeoutError::Disconnected) => break,
                    }
                }
                util::thread_cpu_now() - start
            });
            let output_live = live.clone();
            let sink = move |_: IfaceId, _: PeerId, packet: &OutPacket<'_>| {
                if tx.send(vec![packet.to_vec()]).is_err() {
                    return SendResult::Full;
                }
                output_live.pkts_out.fetch_add(1, Relaxed);
                SendResult::Accepted
            };
            let plan = self.plan.clone();
            let ports = plan.as_ref().map_or(1, |p| p.ports);
            let (handle, mut acceptor, ids, task, ()) = tokio_adapter::spawn_with_source_factory(
                stack_cfg(&self.opts), StreamConfig::default(), vec![IfaceConfig { mtu: tun::MTU as u16 }; ports as usize],
                move |_| (sink, ()), global.clone(), ResourceLimits { port_bytes, peer_bytes, peer_max_connections: 4096 }, input_rx,
                move |shard, now, batch: Batch| {
                    for packet in batch {
                        let location = if plan.is_some() { isolate::classify_packet(&packet).map(|(port, peer)| (IfaceId(port), PeerId(peer))) }
                            else { Some((IfaceId(0), PeerId(1))) };
                        if let Some((iface, peer)) = location {
                            shard.ingress_borrowed(now, iface, peer, &packet);
                            live.pkts_in.fetch_add(1, Relaxed);
                        }
                    }
                },
            );
            for id in ids {
                handle.set_iface_egress_rate(id, self.opts.egress_rate);
                if self.opts.adaptive_egress { handle.set_iface_adaptive_egress(id, true); }
            }
            let errors = Arc::new(Mutex::new(Vec::<String>::new()));
            let task_errors = errors.clone();
            let app_stop = stop.clone();
            let counters = self.counters.clone();
            let splice = self.splice;
            let apps = tokio::spawn(async move {
                let mut workers = tokio::task::JoinSet::new();
                while let Some(stream) = acceptor.accept().await {
                    let errors = task_errors.clone(); let stop = app_stop.clone(); let counters = counters.clone();
                    workers.spawn(async move {
                        let result = if splice {
                            match tokio::net::TcpStream::connect("127.0.0.1:5201").await {
                                Ok(upstream) => stream.splice(upstream).await.map(|_| ()),
                                Err(e) => Err(e),
                            }
                        } else {
                            app(stream, counters).await
                        };
                        if let Err(e) = result {
                            if !stop.load(Relaxed) && !matches!(e.kind(), io::ErrorKind::ConnectionReset | io::ErrorKind::BrokenPipe | io::ErrorKind::ConnectionAborted) {
                                errors.lock().unwrap().push(e.to_string());
                            }
                        }
                    });
                }
                while let Some(result) = workers.join_next().await {
                    if let Err(e) = result { task_errors.lock().unwrap().push(e.to_string()); }
                }
            });
            // Teardown observation only; packet/timer servicing is exclusively
            // the production adapter's event-driven loop.
            let trace = crate::adaptive_fixture::Controls::from_env().expect("validated fixture controls").trace;
            let mut adaptive_trace = Vec::new();
            let mut next_trace = Duration::ZERO;
            while !stop.load(Relaxed) {
                tokio::time::sleep(Duration::from_millis(50)).await;
                if trace {
                    if let Some(start) = crate::adaptive_fixture::window() {
                        let elapsed = start.elapsed();
                        if elapsed >= next_trace {
                            let s = handle.snapshot().await.expect("trace driver live");
                            adaptive_trace.push(serde_json::json!({"elapsed_ms":elapsed.as_secs_f64()*1000.0,
                                "rate":s.stats.adaptive_egress_last_rate,"delivery_rate":s.stats.adaptive_egress_last_delivery_rate,
                                "queue_us":s.stats.adaptive_egress_last_queue_us,"updates":s.stats.adaptive_egress_updates,
                                "probes":s.stats.adaptive_egress_probes,"paced_bytes":s.stats.egress_paced_bytes}));
                            next_trace = elapsed + Duration::from_secs(1);
                        }
                    }
                }
            }
            let snapshot = handle.snapshot().await.expect("driver ended before the fixture");
            task.shutdown_and_join().await.unwrap();
            apps.await.unwrap();
            drop(handle);
            let bridge_cpu = bridge.await.unwrap();
            upstream_stop.store(true, Relaxed);
            if let Some(upstream) = &upstream {
                for _ in 0..300 {
                    if upstream.active.load(Relaxed) == 0 { break; }
                    tokio::time::sleep(Duration::from_millis(10)).await;
                }
            }
            serde_json::json!({"impl":"zfstack-tokio", "cpu_scope":"stack thread includes production Tokio driver and apps; bridge is separate and included in process CPU",
                "bridge_cpu_total_sec":bridge_cpu,"fixture_app_errors":*errors.lock().unwrap(),
                "adaptive_trace":adaptive_trace,
                "production_splice":splice,"kernel_upstream_threads_after_shutdown":upstream.as_ref().map_or(0,|u|u.active.load(Relaxed)),
                "port_physical_bytes":snapshot.port_physical_bytes,"global_reserved_bytes":snapshot.global_reserved_bytes,
                "shards":[{"shard":0,"runs":snapshot.stats.runs,"sink_full":snapshot.stats.sink_full,
                    "egress_paced_bytes":snapshot.stats.egress_paced_bytes,"egress_pacing_pauses":snapshot.stats.egress_pacing_pauses,
                    "adaptive_egress_updates":snapshot.stats.adaptive_egress_updates,"adaptive_egress_probes":snapshot.stats.adaptive_egress_probes,
                    "adaptive_egress_last_rate":snapshot.stats.adaptive_egress_last_rate,"adaptive_egress_last_delivery_rate":snapshot.stats.adaptive_egress_last_delivery_rate,
                    "adaptive_egress_last_queue_us":snapshot.stats.adaptive_egress_last_queue_us,
                    "conns":snapshot.active_connections}],
                "counter_note":"outer bare-host wakeup/poll counters are not available for the native Tokio runtime"})
        });
        drop(rt);
        stats["reserved_after_shutdown"] = serde_json::json!(global.reserved());
        StackResult { cpu_sec: util::thread_cpu_now() - cpu0, stats }
    }
}
