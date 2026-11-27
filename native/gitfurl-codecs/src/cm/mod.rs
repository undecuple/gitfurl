//! The cm stage: a context-mixing LZ decoder for one external FreeArc method, written from a
//! behaviour-only specification (clean room). Its stream is a header and a list of
//! independent jobs; each job is a stream of segment headers (stream A) and a stream of
//! coded segments (stream B), and decodes to at most 512 MiB.
//!
//! Every bound is checked and nothing panics on bad data: damaged input gives
//! `CmError::Corrupt`, a variant this decoder does not cover gives `CmError::Unsupported`.
//! Each coded segment and each stream A must end with the rANS state at its initial value
//! and with exactly its bytes used.
#![forbid(unsafe_code)]

pub mod input;
pub mod job;
pub mod pixel;
pub mod rans;
#[cfg(test)]
mod testkit;
#[cfg(test)]
mod tests;
pub mod texture;

use std::fmt;

use input::{Fetch, Input, SliceFetch};
use job::JobState;
use rans::{Coder, Rans, RANS_LOW};

/// Bytes of the stream header (multi-job form).
pub const HEADER_LEN: usize = 11;
/// The largest job output (the header's job size field allows no more).
pub const MAX_JOB: usize = 256 << 21;

#[derive(Debug)]
pub enum CmError {
    /// The data is damaged (or was not made by a matching encoder).
    Corrupt(&'static str),
    /// A variant of the format this decoder does not cover.
    Unsupported(&'static str),
    /// The data source failed or asked to stop (the caller holds the reason).
    External,
}

impl fmt::Display for CmError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            CmError::Corrupt(r) => write!(f, "damaged data ({r})"),
            CmError::Unsupported(r) => write!(f, "a variant gitfurl does not decode ({r})"),
            CmError::External => write!(f, "the data could not be read"),
        }
    }
}

impl std::error::Error for CmError {}

/// The header fields the decoder uses.
#[derive(Clone, Copy, Debug)]
pub struct Params {
    pub sh_kind: usize,
    pub sh_w: usize,
    pub sh_lo: usize,
    pub sh_hm: usize,
    pub sh_hi: usize,
    pub a6: u32,
    pub a7: u32,
    /// window bytes (`win << 24`)
    pub window: u64,
    /// output bytes of every job but the last
    pub job_size: usize,
}

/// Reads and checks the 11-byte header (sec. 2); refuses every variant not covered.
pub fn parse_header(h: &[u8]) -> Result<Params, CmError> {
    if h.len() < HEADER_LEN {
        return Err(CmError::Corrupt("the header is cut short"));
    }
    let a = u32::from_le_bytes([h[0], h[1], h[2], h[3]]);
    let b = u32::from_le_bytes([h[4], h[5], h[6], h[7]]);
    let c = h[8];
    let field = |v: u32, lo: u32, bits: u32| (v >> lo) & ((1 << bits) - 1);
    if a >> 31 == 0 {
        return Err(CmError::Unsupported("the single-job form"));
    }
    if field(a, 30, 1) == 0 {
        return Err(CmError::Unsupported("the other decoder flavour"));
    }
    if field(b, 8, 3) != 0 {
        return Err(CmError::Unsupported("a larger literal model"));
    }
    if field(b, 11, 2) != 0 {
        return Err(CmError::Unsupported("the byte-position model"));
    }
    if c & 1 != 0 {
        return Err(CmError::Unsupported("the long-range pre-pass"));
    }
    if (c >> 1) & 7 != 0 {
        return Err(CmError::Unsupported(
            "image sub-coder settings other than the default",
        ));
    }
    let a0 = field(a, 0, 4);
    let a1 = field(a, 4, 4);
    let a2 = field(a, 8, 4);
    let a3 = field(a, 12, 4);
    let a4 = field(a, 16, 4);
    if a0 > 6 || a1 > 6 || a3 > 6 || a2 > 8 || a4 > 8 {
        return Err(CmError::Unsupported("context shifts out of range"));
    }
    let win = field(b, 13, 7) as u64;
    if win == 0 {
        return Err(CmError::Unsupported("an empty window"));
    }
    let window = win << 24;
    let job_size = (field(b, 24, 8) as usize + 1) << 21;
    if job_size as u64 > window {
        return Err(CmError::Unsupported("jobs larger than the window"));
    }
    Ok(Params {
        sh_kind: (8 - a0) as usize,
        sh_w: (8 - a1) as usize,
        sh_lo: (8 - a2) as usize,
        sh_hm: (8 - a3) as usize,
        sh_hi: (8 - a4) as usize,
        a6: field(a, 23, 3),
        a7: field(a, 26, 3),
        window,
        job_size,
    })
}

const TYPE1: [u8; 15] = [
    0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x0C, 0x0D, 0x0E, 0x0F, 0x3F, 0x3E,
];
const TYPE2: [u8; 9] = [0x10, 0x11, 0x12, 0x13, 0x20, 0x21, 0x22, 0x23, 0x24];
pub const STORED: usize = 0x3F;

/// The number model of stream A (sec. 5.4).
struct NumModel {
    hist: usize,
    ctx: usize,
    z: [u16; 4],
    c1: Vec<[u16; 17]>,
    c2: Vec<[u16; 17]>,
    bt: Vec<u16>, // [31][64]
}

impl NumModel {
    fn new() -> Self {
        let mut c = [0u16; 17];
        rans::uniform(&mut c, 16);
        NumModel {
            hist: 0,
            ctx: 0,
            z: [0x4000; 4],
            c1: vec![c; 32],
            c2: vec![c; 32],
            bt: vec![0x4000; 31 * 64],
        }
    }

    fn decode<R: Coder>(&mut self, r: &mut R) -> u64 {
        let b = r.bit15(&mut self.z[self.hist], 4);
        self.hist = (2 * self.hist + b) & 3;
        if b == 0 {
            self.ctx = 0;
            return 0;
        }
        let mut c = r.sym(&mut self.c1[self.ctx], 16, 2, 5);
        if c == 15 {
            c = 15 + r.sym(&mut self.c2[self.ctx], 16, 2, 5);
        }
        let mut v: u64 = 1;
        let mut t = 1usize;
        for _ in 0..c {
            let e = r.bit15(&mut self.bt[c * 64 + t], 4);
            t = (2 * t + e) & 63;
            v = 2 * v + e as u64;
        }
        self.ctx = c + 1;
        v
    }
}

/// The segment-header decoder of one job (sec. 4).
struct Headers {
    flag: [u16; 2],
    fctx: usize,
    t1: [u16; 17],
    t2: [u16; 17],
    len: NumModel,
    clen: NumModel,
    par: NumModel,
    prevtype: usize,
}

struct SegHead {
    ty: usize,
    len: u64,
    clen: u64,
    par: u64,
}

impl Headers {
    fn new() -> Self {
        let mut c = [0u16; 17];
        rans::uniform(&mut c, 16);
        Headers {
            flag: [0x4000; 2],
            fctx: 0,
            t1: c,
            t2: c,
            len: NumModel::new(),
            clen: NumModel::new(),
            par: NumModel::new(),
            prevtype: 0,
        }
    }

    fn next<R: Coder>(&mut self, r: &mut R) -> Result<SegHead, CmError> {
        let f = r.bit15(&mut self.flag[self.fctx], 5);
        self.fctx = f;
        let ty = if f == 0 {
            let s = r.sym(&mut self.t1, 16, 2, 5);
            if s < 15 {
                TYPE1[s] as usize
            } else {
                let s2 = r.sym(&mut self.t2, 16, 2, 5);
                *TYPE2
                    .get(s2)
                    .ok_or(CmError::Corrupt("an unknown segment type"))? as usize
            }
        } else {
            self.prevtype
        };
        self.prevtype = ty;
        let len = self.len.decode(r);
        let (clen, par) = if ty == STORED {
            (len, 0)
        } else {
            let clen = self.clen.decode(r);
            let par = if (1..=11).contains(&ty) {
                self.par.decode(r)
            } else {
                0
            };
            (clen, par)
        };
        Ok(SegHead { ty, len, clen, par })
    }
}

fn supported_type(ty: usize) -> Result<(), CmError> {
    match ty {
        0..=7 | 0x0C..=0x0F | STORED => Ok(()),
        0x20..=0x24 => Err(CmError::Unsupported("special modes K9-K13")),
        _ => Err(CmError::Unsupported(
            "a segment type gitfurl does not decode",
        )),
    }
}

/// Where one job's two streams are in the compressed data.
#[derive(Clone, Copy, Debug)]
pub struct JobSpan {
    pub a_off: u64,
    pub a_len: u64,
    pub b_off: u64,
    pub b_len: u64,
}

/// Segments between two checks of the data source's stop request.
const CHECK_EVERY: u32 = 64;

/// Decodes one job into `out` (at least `p.job_size` bytes; positions not produced are left
/// as they are). Returns the bytes produced: `job_size`, or fewer for a job that ends with
/// an end-of-job header (only the stream's last job may).
pub fn decode_job(
    p: &Params,
    span: &JobSpan,
    fetch: &mut dyn Fetch,
    out: &mut [u8],
) -> Result<usize, CmError> {
    decode_job_seen(p, span, fetch, out, &mut |_, _, _| {})
}

/// decode_job, calling `seen_seg(type, start, bytes)` with each segment once it is decoded.
pub fn decode_job_seen(
    p: &Params,
    span: &JobSpan,
    fetch: &mut dyn Fetch,
    out: &mut [u8],
    seen_seg: &mut dyn FnMut(usize, usize, &[u8]),
) -> Result<usize, CmError> {
    let jobsize = p.job_size;
    if out.len() < jobsize {
        return Err(CmError::Corrupt("an output buffer smaller than a job"));
    }
    let out = &mut out[..jobsize];
    let mut heads = Headers::new();
    let mut job = JobState::new(out, *p);
    // stream A is small next to stream B; it is read whole, under its own cap
    if span.a_len > 64 << 20 {
        return Err(CmError::Corrupt(
            "a segment-header stream larger than gitfurl reads",
        ));
    }
    let mut a_buf = vec![0u8; span.a_len as usize];
    Input::new(fetch, span.a_off, span.a_len).copy_to(&mut a_buf)?;
    let mut a_src = SliceFetch(&a_buf);
    let mut ain = Input::new(&mut a_src, 0, span.a_len);
    let mut ar = Rans::new(&mut ain);
    let mut bin = Input::new(fetch, span.b_off, span.b_len);
    let mut produced: usize = 0;
    let mut seen: u32 = 0;
    let mut ended = false;
    while produced < jobsize {
        seen += 1;
        if seen.is_multiple_of(CHECK_EVERY) {
            bin.check()?;
        }
        let h = heads.next(&mut ar)?;
        supported_type(h.ty)?;
        if h.ty == STORED && h.len == 0 {
            ended = true;
            break;
        }
        if h.len == 0 {
            return Err(CmError::Corrupt("an empty segment"));
        }
        if h.len > (jobsize - produced) as u64 {
            return Err(CmError::Corrupt("a segment past the end of its job"));
        }
        if h.clen > bin.remaining() {
            return Err(CmError::Corrupt("a segment longer than the job's data"));
        }
        let len = h.len as usize;
        bin.limit = h.clen;
        if h.ty == STORED {
            bin.copy_to(&mut job.out[produced..produced + len])?;
            job.pos += len;
        } else {
            if h.clen < 4 {
                return Err(CmError::Corrupt(
                    "a coded segment shorter than its coder state",
                ));
            }
            job.coded_segment(h.ty, h.par, len, &mut bin)?;
        }
        if job.pos != produced + len {
            return Err(CmError::Corrupt(
                "a segment that did not give its stated size",
            ));
        }
        seen_seg(h.ty, produced, &job.out[produced..produced + len]);
        produced += len;
    }
    if !ended {
        // a full job still ends with an end-of-job header
        let h = heads.next(&mut ar)?;
        if h.ty != STORED || h.len != 0 {
            return Err(CmError::Corrupt("a job without its end-of-job header"));
        }
    }
    let (x, bad) = (ar.x, ar.bad);
    if bad || ain.overrun || x != RANS_LOW || ain.remaining() != 0 {
        return Err(CmError::Corrupt(
            "the segment headers failed their end check",
        ));
    }
    if bin.remaining() != 0 {
        return Err(CmError::Corrupt("unused data at the end of a job"));
    }
    Ok(produced)
}

/// The jobs of a whole stream held in memory: header checks and the container walk.
pub fn walk(data: &[u8]) -> Result<(Params, Vec<JobSpan>), CmError> {
    let p = parse_header(data)?;
    let mut at = HEADER_LEN as u64;
    let n = data.len() as u64;
    let u32_at = |q: u64| -> Result<u64, CmError> {
        let q = q as usize;
        let b = data
            .get(q..q + 4)
            .ok_or(CmError::Corrupt("the job list is cut short"))?;
        Ok(u32::from_le_bytes([b[0], b[1], b[2], b[3]]) as u64)
    };
    let mut jobs = Vec::new();
    loop {
        let la = u32_at(at)?;
        at += 4;
        if la == 0 {
            break;
        }
        if la > n - at {
            return Err(CmError::Corrupt("a job longer than the data"));
        }
        let a_off = at;
        at += la;
        let lb = u32_at(at)?;
        at += 4;
        if lb > n - at {
            return Err(CmError::Corrupt("a job longer than the data"));
        }
        jobs.push(JobSpan {
            a_off,
            a_len: la,
            b_off: at,
            b_len: lb,
        });
        at += lb;
    }
    if at != n {
        return Err(CmError::Corrupt("data after the end of the stream"));
    }
    Ok((p, jobs))
}

/// Decodes the jobs `spans` on up to `threads` threads at once; each output is
/// `p.job_size` bytes long, and the second value is how many of them were produced.
pub fn decode_jobs<F, M>(
    p: &Params,
    spans: &[JobSpan],
    outs: &mut [&mut [u8]],
    threads: usize,
    make: M,
) -> Vec<Result<usize, CmError>>
where
    F: Fetch,
    M: Fn(usize) -> F + Sync,
{
    let threads = threads.max(1);
    let mut results: Vec<Option<Result<usize, CmError>>> = (0..spans.len()).map(|_| None).collect();
    if threads == 1 || spans.len() <= 1 {
        for (i, (span, out)) in spans.iter().zip(outs.iter_mut()).enumerate() {
            let mut f = make(i);
            results[i] = Some(decode_job(p, span, &mut f, out));
        }
    } else {
        let work: Vec<(usize, &JobSpan, &mut &mut [u8])> = spans
            .iter()
            .zip(outs.iter_mut())
            .enumerate()
            .map(|(i, (s, o))| (i, s, o))
            .collect();
        let next = std::sync::Mutex::new(work.into_iter());
        let done = std::sync::Mutex::new(&mut results);
        std::thread::scope(|sc| {
            for _ in 0..threads.min(spans.len()) {
                sc.spawn(|| loop {
                    let item = match next.lock() {
                        Ok(mut it) => it.next(),
                        Err(_) => None,
                    };
                    let Some((i, span, out)) = item else { break };
                    let mut f = make(i);
                    let r = decode_job(p, span, &mut f, out);
                    if let Ok(mut d) = done.lock() {
                        d[i] = Some(r);
                    }
                });
            }
        });
    }
    results
        .into_iter()
        .map(|r| r.unwrap_or(Err(CmError::External)))
        .collect()
}

/// Decodes a whole stream held in memory (tests and small streams).
pub fn decode(data: &[u8], threads: usize) -> Result<Vec<u8>, CmError> {
    let (p, spans) = walk(data)?;
    let mut out = Vec::new();
    let threads = threads.max(1);
    for (k, group) in spans.chunks(threads).enumerate() {
        let mut bufs: Vec<Vec<u8>> = group.iter().map(|_| vec![0u8; p.job_size]).collect();
        let mut views: Vec<&mut [u8]> = bufs.iter_mut().map(|b| b.as_mut_slice()).collect();
        let res = decode_jobs(&p, group, &mut views, threads, |_| SliceFetch(data));
        for (j, (r, b)) in res.into_iter().zip(bufs.iter()).enumerate() {
            let n = r?;
            let last = k * threads + j + 1 == spans.len();
            if n < p.job_size && !last {
                return Err(CmError::Corrupt("a short job before the last"));
            }
            out.extend_from_slice(&b[..n]);
        }
    }
    Ok(out)
}
