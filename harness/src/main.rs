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
        #[arg(long, value_enum, default_value = "json")]
        format: Format,
        /// Also write the JSON document here.
        #[arg(long)]
        json: Option<PathBuf>,
    },
    /// Print scenario names.
    List,
}

#[derive(Copy, Clone, ValueEnum)]
enum Format {
    Json,
    Md,
}

fn main() {
    let cli = Cli::parse();
    match cli.cmd {
        Cmd::List => {
            for n in scenario_names() {
                println!("{n}");
            }
        }
        Cmd::L0 { scenario, seed, ports, lights, heavy_bytes, heavy_conns, until_ms, full, tx_cache_trim, format, json } => {
            let over = Overrides { seed: Some(seed), ports, lights, heavy_bytes, heavy_conns, until_ms, full, tx_cache_trim };
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
                obj.insert("layer".into(), serde_json::json!("L0"));
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
