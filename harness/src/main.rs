//! `zfharness l0` runs the sans-IO multi-tenant scenarios. No root, no TUN, no WireGuard.

use std::path::PathBuf;
use std::process::Command;

use clap::{Parser, Subcommand, ValueEnum};
use zfharness::{run_named, scenario_names, Overrides};

#[derive(Parser)]
#[command(name = "zfharness", about = "zfstack multi-tenant harness (L0 sans-IO; WireGuard is not on this path)")]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Run one L0 scenario, or `matrix`.
    L0 {
        /// Scenario name. `zfharness list` prints them. `matrix` runs the minimum set.
        #[arg(long, default_value = "matrix")]
        scenario: String,
        #[arg(long, default_value_t = 1)]
        seed: u64,
        #[arg(long, default_value = "eager", value_parser = ["eager", "event-only"])]
        app_drive: String,
        /// Logical ports. Honored by port-density and the noisy/fanout scenarios.
        #[arg(long)]
        ports: Option<u16>,
        #[arg(long)]
        lights: Option<u16>,
        #[arg(long)]
        heavy_bytes: Option<u64>,
        #[arg(long)]
        heavy_conns: Option<u16>,
        /// Override the virtual deadline, milliseconds from the origin.
        #[arg(long)]
        until_ms: Option<u64>,
        /// Matrix also runs port-density at 64 and 128.
        #[arg(long)]
        full: bool,
        /// Exercise opt-in server TX trimming (10/20/60 ms, 2 large/1 small).
        #[arg(long)]
        tx_cache_trim: bool,
        #[arg(long, default_value = "off", value_parser = ["off", "drr-max", "drr-16k", "drr-2mss"])]
        sched_mode: String,
        #[arg(long, value_enum, default_value = "json")]
        format: Format,
        /// Also write the JSON document here.
        #[arg(long)]
        json: Option<PathBuf>,
    },
    /// Print scenario names.
    List {
        #[arg(long, value_enum)]
        format: Option<Format>,
    },
    /// Execute a bounded, versioned event script against logical flow targets.
    Replay {
        #[arg(long)]
        script: PathBuf,
        #[arg(long)]
        json: Option<PathBuf>,
        /// Save the script and bounded failure trace here on failure.
        #[arg(long)]
        failure_dir: Option<PathBuf>,
    },
    /// Delete irrelevant events while preserving a reached failing assertion.
    Reduce {
        #[arg(long)]
        script: PathBuf,
        #[arg(long)]
        assertion: String,
        #[arg(long)]
        output: PathBuf,
    },
}

#[derive(Copy, Clone, ValueEnum)]
enum Format {
    Json,
    Md,
}

fn main() {
    let cli = Cli::parse();
    match cli.cmd {
        Cmd::List { format } => {
            if matches!(format, Some(Format::Json)) {
                println!(
                    "{}",
                    serde_json::json!({"schema_version": 1, "scenarios": scenario_names().iter().map(|n|
                    serde_json::json!({"name": n, "contract_ids": zfharness::oracle::contract_ids(n), "app_drive_modes": ["eager", "event-only"]})).collect::<Vec<_>>()})
                );
            } else {
                for n in scenario_names() {
                    println!("{n}");
                }
            }
        }
        Cmd::Replay { script, json, failure_dir } => {
            let input = read_script(&script);
            let mut value = zfharness::event::run(&input).unwrap_or_else(|e| invalid(&e));
            value["tool"] = serde_json::json!("zfharness");
            value["git_sha"] = serde_json::json!(git_sha());
            value["layer"] = serde_json::json!(if value["app_drive"] == "event-only" { "L0-E" } else { "L0-C" });
            let text = serde_json::to_string_pretty(&value).unwrap();
            if let Some(path) = json {
                write_output(&path, &text);
            }
            println!("{text}");
            if value["ok"] != true {
                let dir = failure_dir.unwrap_or_else(|| std::env::temp_dir().join(format!("zfharness-failure-{}", std::process::id())));
                std::fs::create_dir_all(&dir).unwrap_or_else(|e| invalid(&format!("creating {}: {e}", dir.display())));
                write_output(&dir.join("case.json"), &input);
                write_output(&dir.join("result.json"), &text);
                write_output(
                    &dir.join("identity.json"),
                    &serde_json::json!({"git_sha":git_sha(), "argv":std::env::args().collect::<Vec<_>>(),"replay_schema":1}).to_string(),
                );
                eprintln!("failure corpus: {} (replay with zfharness replay --script {}/case.json)", dir.display(), dir.display());
                std::process::exit(1);
            }
        }
        Cmd::Reduce { script, assertion, output } => {
            let reduced = zfharness::event::reduce(&read_script(&script), &assertion).unwrap_or_else(|e| invalid(&e));
            write_output(&output, &serde_json::to_string_pretty(&reduced).unwrap());
        }
        Cmd::L0 { scenario, seed, app_drive, ports, lights, heavy_bytes, heavy_conns, until_ms, full, tx_cache_trim, sched_mode, format, json } => {
            let over = Overrides {
                app_drive: zfharness::schedule::AppDriveMode::parse(&app_drive).unwrap(),
                seed: Some(seed),
                ports,
                lights,
                heavy_bytes,
                heavy_conns,
                until_ms,
                full,
                tx_cache_trim,
                sched_mode: sched_mode.clone(),
            };
            let result = run_named(&scenario, &over);
            let mut value = match result {
                Ok(v) => v,
                Err(e) => {
                    eprintln!("zfharness: {e}");
                    std::process::exit(2);
                }
            };
            if let Some(obj) = value.as_object_mut() {
                obj.insert("tool".into(), serde_json::json!("zfharness"));
                obj.insert("app_drive".into(), serde_json::json!(app_drive));
                obj.insert("layer".into(), serde_json::json!(if app_drive == "event-only" { "L0-E" } else { "L0-C" }));
                obj.insert("sched_mode".into(), serde_json::json!(sched_mode));
                obj.insert("tx_cache_trim_enabled".into(), serde_json::json!(tx_cache_trim));
                obj.insert("git_sha".into(), serde_json::json!(git_sha()));
                obj.insert("l1".into(), serde_json::json!("zfbench --test isolate (root, TUN, no WireGuard)"));
                obj.insert("l2".into(), serde_json::json!("not run; WireGuard is out of the core matrix"));
            }
            let text = serde_json::to_string_pretty(&value).unwrap();
            if let Some(path) = &json {
                if let Err(e) = std::fs::write(path, &text) {
                    eprintln!("zfharness: writing {}: {e}", path.display());
                    std::process::exit(3);
                }
            }
            match format {
                Format::Json => println!("{text}"),
                Format::Md => print!("{}", zfharness::report::to_markdown(&value)),
            }
            if !value["ok"].as_bool().unwrap_or(false) {
                std::process::exit(1);
            }
        }
    }
}

fn git_sha() -> String {
    Command::new("git")
        .args(["rev-parse", "HEAD"])
        .output()
        .ok()
        .and_then(|o| String::from_utf8(o.stdout).ok())
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "unknown".into())
}

fn invalid(message: &str) -> ! {
    eprintln!("zfharness: {message}");
    std::process::exit(2)
}

fn read_script(path: &std::path::Path) -> String {
    let metadata = std::fs::metadata(path).unwrap_or_else(|e| invalid(&format!("reading {}: {e}", path.display())));
    if metadata.len() > zfharness::event::MAX_SCRIPT_BYTES as u64 {
        invalid("script exceeds 1 MiB limit");
    }
    std::fs::read_to_string(path).unwrap_or_else(|e| invalid(&format!("reading {}: {e}", path.display())))
}

fn write_output(path: &std::path::Path, text: &str) {
    if let Err(e) = std::fs::write(path, text) {
        eprintln!("writing {}: {e}", path.display());
        std::process::exit(3);
    }
}
