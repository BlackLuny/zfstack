//! Opt-in, bounded test-side trace of isolate light peers only. Never in the library.
use std::fs::{File, OpenOptions};
use std::io::{BufWriter, Write};
use std::net::TcpStream;
use std::sync::{Mutex, OnceLock};
use std::time::Instant;
use serde_json::{json, Value};

struct Trace { start: Instant, rows: Mutex<(Option<File>, Vec<Value>, usize)> }
static TRACE: OnceLock<Option<Trace>> = OnceLock::new();
fn trace() -> Option<&'static Trace> {
    TRACE.get_or_init(|| std::env::var_os("ZFBENCH_TAIL_TRACE").map(|path| {
        let file = OpenOptions::new().write(true).create_new(true).open(path).expect("fresh tail trace path");
        Trace { start: Instant::now(), rows: Mutex::new((Some(file),Vec::new(),0)) }
    })).as_ref()
}
pub fn enabled() -> bool { trace().is_some() }
pub(crate) fn row(mut value: Value) {
    let Some(t) = trace() else { return };
    value["at_ns"] = json!(t.start.elapsed().as_nanos() as u64);
    let mut guard = t.rows.lock().unwrap();
    if guard.1.len() >= 200_000 { guard.2 += 1; return; }
    guard.1.push(value);
}
pub fn packet(stage: &str, p: &[u8], queue_bytes: usize, planned_us: u64) {
    if !enabled() || p.len() < 40 || p[0] >> 4 != 4 || p[9] != 6 { return; }
    let h = ((p[0] & 15) as usize) * 4;
    if h < 20 || p.len() < h + 20 { return; }
    let up = p[12..14] == [10,210];
    let peer = if up {p[14]} else if p[16..18] == [10,210] {p[18]} else {return};
    if peer < 2 { return; }
    let port_at = if up {h} else {h+2};
    let tcp_h = ((p[h+12] >> 4) as usize)*4;
    if tcp_h < 20 || p.len() < h+tcp_h { return; }
    row(json!({"kind":"packet","stage":stage,"peer":peer,"port":u16::from_be_bytes(p[port_at..port_at+2].try_into().unwrap()),
        "dir":if up {"up"} else {"down"},"seq":u32::from_be_bytes(p[h+4..h+8].try_into().unwrap()),
        "ack":u32::from_be_bytes(p[h+8..h+12].try_into().unwrap()),"flags":p[h+13],
        "payload":p.len()-h-tcp_h,"queue_bytes":queue_bytes,"planned_us":planned_us}));
}
pub fn client(stage: &str, stream: &TcpStream) {
    if !enabled() { return; }
    let local = stream.local_addr().unwrap();
    let peer = match local.ip() { std::net::IpAddr::V4(ip) => ip.octets()[2], _ => return };
    row(json!({"kind":"request","stage":stage,"peer":peer,"port":local.port()}));
}
pub fn flush() {
    let Some(t) = trace() else { return };
    let mut g=t.rows.lock().unwrap();
    let Some(file)=g.0.take() else {return};
    // File I/O on the macOS bind mount must not stall measured link threads.
    let mut writer=BufWriter::new(file);
    for value in &g.1 {serde_json::to_writer(&mut writer,value).unwrap();writeln!(writer).unwrap();}
    let value=json!({"kind":"trace_end","rows":g.1.len(),"omitted":g.2});
    serde_json::to_writer(&mut writer,&value).unwrap(); writeln!(writer).unwrap(); writer.flush().unwrap();
}
