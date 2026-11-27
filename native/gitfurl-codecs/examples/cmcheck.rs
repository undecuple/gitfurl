//! Checks the cm-stage decoder against per-segment CRC-32 lists, without writing any decoded
//! byte anywhere: only checksums are printed.
//!
//!     cmcheck ARCHIVE BLOCK_OFFSET ORACLE_DIR THREADS [JOB ...]
//!
//! ORACLE_DIR holds `crc<J>-oracle.txt` files: one line per segment (`index type offset length
//! crc32`, type in decimal, crc in hex), then `size N extra 0` and `sha256 HEX`. Without JOB
//! arguments every job of the block is checked. Prints one line per job and exits 1 on any
//! mismatch.

use std::fs::File;
use std::os::unix::fs::FileExt;
use std::sync::Mutex;
use std::time::Instant;

use gitfurl_codecs::cm::input::Fetch;
use gitfurl_codecs::cm::{decode_job_seen, parse_header, CmError, JobSpan, HEADER_LEN};

struct FileFetch<'a> {
    f: &'a File,
}

impl Fetch for FileFetch<'_> {
    fn fetch(&mut self, off: u64, n: usize, buf: &mut Vec<u8>) -> Result<(), CmError> {
        let start = buf.len();
        buf.resize(start + n, 0);
        self.f
            .read_exact_at(&mut buf[start..], off)
            .map_err(|_| CmError::External)
    }
}

fn crc_table() -> [u32; 256] {
    let mut t = [0u32; 256];
    for (i, v) in t.iter_mut().enumerate() {
        let mut c = i as u32;
        for _ in 0..8 {
            c = if c & 1 != 0 {
                0xEDB8_8320 ^ (c >> 1)
            } else {
                c >> 1
            };
        }
        *v = c;
    }
    t
}

fn crc32(t: &[u32; 256], data: &[u8]) -> u32 {
    let mut c = !0u32;
    for &b in data {
        c = t[((c ^ b as u32) & 0xFF) as usize] ^ (c >> 8);
    }
    !c
}

fn sha256(data: &[u8]) -> String {
    const K: [u32; 64] = [
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4,
        0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe,
        0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f,
        0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
        0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc,
        0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
        0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116,
        0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
        0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7,
        0xc67178f2,
    ];
    let mut h: [u32; 8] = [
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab,
        0x5be0cd19,
    ];
    let mut block = |chunk: &[u8]| {
        let mut w = [0u32; 64];
        for i in 0..16 {
            w[i] = u32::from_be_bytes([
                chunk[4 * i],
                chunk[4 * i + 1],
                chunk[4 * i + 2],
                chunk[4 * i + 3],
            ]);
        }
        for i in 16..64 {
            let s0 = w[i - 15].rotate_right(7) ^ w[i - 15].rotate_right(18) ^ (w[i - 15] >> 3);
            let s1 = w[i - 2].rotate_right(17) ^ w[i - 2].rotate_right(19) ^ (w[i - 2] >> 10);
            w[i] = w[i - 16]
                .wrapping_add(s0)
                .wrapping_add(w[i - 7])
                .wrapping_add(s1);
        }
        let mut v = h;
        for i in 0..64 {
            let s1 = v[4].rotate_right(6) ^ v[4].rotate_right(11) ^ v[4].rotate_right(25);
            let ch = (v[4] & v[5]) ^ (!v[4] & v[6]);
            let t1 = v[7]
                .wrapping_add(s1)
                .wrapping_add(ch)
                .wrapping_add(K[i])
                .wrapping_add(w[i]);
            let s0 = v[0].rotate_right(2) ^ v[0].rotate_right(13) ^ v[0].rotate_right(22);
            let maj = (v[0] & v[1]) ^ (v[0] & v[2]) ^ (v[1] & v[2]);
            let t2 = s0.wrapping_add(maj);
            v = [
                t1.wrapping_add(t2),
                v[0],
                v[1],
                v[2],
                v[3].wrapping_add(t1),
                v[4],
                v[5],
                v[6],
            ];
        }
        for i in 0..8 {
            h[i] = h[i].wrapping_add(v[i]);
        }
    };
    let full = data.len() / 64 * 64;
    for c in data[..full].chunks(64) {
        block(c);
    }
    let mut tail = data[full..].to_vec();
    tail.push(0x80);
    while tail.len() % 64 != 56 {
        tail.push(0);
    }
    tail.extend_from_slice(&((data.len() as u64) * 8).to_be_bytes());
    for c in tail.chunks(64) {
        block(c);
    }
    h.iter().map(|v| format!("{v:08x}")).collect()
}

struct Oracle {
    segs: Vec<(usize, usize, usize, u32)>, // type, offset, length, crc
    size: usize,
    sha: String,
}

fn read_oracle(path: &str) -> Option<Oracle> {
    let text = std::fs::read_to_string(path).ok()?;
    let mut o = Oracle {
        segs: Vec::new(),
        size: 0,
        sha: String::new(),
    };
    for line in text.lines() {
        let f: Vec<&str> = line.split_whitespace().collect();
        match f.first() {
            Some(&"size") => o.size = f.get(1)?.parse().ok()?,
            Some(&"sha256") => o.sha = f.get(1)?.to_string(),
            Some(_) if f.len() == 5 => o.segs.push((
                f[1].parse().ok()?,
                f[2].parse().ok()?,
                f[3].parse().ok()?,
                u32::from_str_radix(f[4], 16).ok()?,
            )),
            _ => {}
        }
    }
    Some(o)
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() < 5 {
        eprintln!("usage: cmcheck ARCHIVE BLOCK_OFFSET ORACLE_DIR THREADS [JOB ...]");
        std::process::exit(2);
    }
    let f = File::open(&args[1]).expect("open the archive");
    let base: u64 = args[2].parse().expect("block offset");
    let odir = &args[3];
    let threads: usize = args[4].parse().expect("threads");
    let mut head = [0u8; HEADER_LEN];
    f.read_exact_at(&mut head, base).expect("read the header");
    let p = parse_header(&head).unwrap_or_else(|e| panic!("header: {e}"));
    let mut spans = Vec::new();
    let mut at = base + HEADER_LEN as u64;
    let u32_at = |q: u64| {
        let mut b = [0u8; 4];
        f.read_exact_at(&mut b, q).expect("read a job length");
        u32::from_le_bytes(b) as u64
    };
    loop {
        let la = u32_at(at);
        if la == 0 {
            break;
        }
        let lb = u32_at(at + 4 + la);
        spans.push(JobSpan {
            a_off: at + 4,
            a_len: la,
            b_off: at + 8 + la,
            b_len: lb,
        });
        at += 8 + la + lb;
    }
    let jobs: Vec<usize> = if args.len() > 5 {
        args[5..].iter().map(|s| s.parse().expect("job")).collect()
    } else {
        (0..spans.len()).collect()
    };
    let table = crc_table();
    let queue = Mutex::new(jobs.clone().into_iter());
    let failed = Mutex::new(false);
    let t0 = Instant::now();
    let total = Mutex::new(0usize);
    std::thread::scope(|sc| {
        for _ in 0..threads.max(1) {
            sc.spawn(|| loop {
                let Some(j) = queue.lock().unwrap().next() else { break };
                let oracle = read_oracle(&format!("{odir}/crc{j}-oracle.txt"));
                let mut out = vec![0u8; p.job_size];
                let mut idx = 0usize;
                let mut first_bad: Option<String> = None;
                let start = Instant::now();
                let r = decode_job_seen(&p, &spans[j], &mut FileFetch { f: &f }, &mut out, &mut |ty, off, seg| {
                    if let Some(o) = &oracle {
                        let c = crc32(&table, seg);
                        let ok = o.segs.get(idx).is_some_and(|e| *e == (ty, off, seg.len(), c));
                        if !ok && first_bad.is_none() {
                            first_bad = Some(format!("segment {idx} type {ty:#x} at {off} len {} crc {c:08x}, expected {:?}", seg.len(), o.segs.get(idx)));
                        }
                    }
                    idx += 1;
                });
                let secs = start.elapsed().as_secs_f64();
                let line = match r {
                    Ok(n) => {
                        let sha = sha256(&out[..n]);
                        let good = oracle.as_ref().is_some_and(|o| o.size == n && o.sha == sha && first_bad.is_none());
                        if !good {
                            *failed.lock().unwrap() = true;
                        }
                        *total.lock().unwrap() += n;
                        format!(
                            "job {j}: {} {n} bytes, {idx} segments, sha256 {sha}, {secs:.1} s, {:.1} MB/s{}",
                            if good { "MATCH" } else { "MISMATCH" },
                            n as f64 / secs / 1e6,
                            first_bad.map(|b| format!(" (first bad: {b})")).unwrap_or_default()
                        )
                    }
                    Err(e) => {
                        *failed.lock().unwrap() = true;
                        format!(
                            "job {j}: ERROR {e} in segment {idx} after {secs:.1} s{}",
                            first_bad.map(|b| format!(" (first bad: {b})")).unwrap_or_default()
                        )
                    }
                };
                println!("{line}");
            });
        }
    });
    let secs = t0.elapsed().as_secs_f64();
    let n = *total.lock().unwrap();
    println!(
        "total: {n} bytes in {secs:.1} s, {:.1} MB/s with {threads} thread(s)",
        n as f64 / secs / 1e6
    );
    if *failed.lock().unwrap() {
        std::process::exit(1);
    }
}
