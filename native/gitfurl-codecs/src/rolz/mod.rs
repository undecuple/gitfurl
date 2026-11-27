//! The rolz stage: a port of gitfurl's pure-Python decoder (src/gitfurl/farc_rolz.py, the
//! reference), which was written from a behaviour-only specification (clean room). A block
//! holds records; each record is a container of CRC-checked packets whose streams are read
//! by a two-state rANS reader, decoded by an LZ with reduced-offset matches and special
//! blocks, then an x86 call stage and long-range copies. Every check and limit of the
//! Python decoder is kept, with the same refusal texts.
//!
//! Nothing here panics on bad data: every size and index read from the stream is checked
//! before it is used, and large buffers are reserved fallibly (`RolzError::Memory`).
#![forbid(unsafe_code)]

pub mod calls;
pub mod coder;
pub mod container;
pub mod lz;
#[cfg(test)]
mod testkit;
#[cfg(test)]
mod tests;

use std::borrow::Cow;
use std::fmt;
use std::sync::OnceLock;

use calls::X86Calls;
use coder::{states, Coder, States};
use container::Container;
use lz::Lz;

pub const M32: u64 = 0xFFFF_FFFF;
pub const MAGIC: [u8; 4] = [0x43, 0x4d, 0x28, 0x05];
/// Zero bytes of history in front of every LZ's output.
pub const HIST: usize = 0xFF0;
/// The directory's and the table LZ's window.
pub const SMALL_WINDOW: u64 = 1 << 20;
/// A larger window (from the directory) is refused.
pub const WINDOW_CAP: u64 = 512 << 20;
/// The directory's decoded size.
pub const DIR_CAP: usize = 1 << 20;
/// Chunks in one container.
pub const MAX_CHUNKS: usize = 1 << 16;
/// Bytes of one container's index.
pub const MAX_INDEX: usize = 1 << 20;
/// The x86 stage's request size.
pub const REQUEST: u64 = 65536;
/// A small-window LZ drops its oldest output this much past its window.
pub const TRIM_SLACK: usize = 1 << 20;
/// LZ tokens between two Stop checks.
pub const STOP_TOKENS: u64 = 1 << 15;
/// Both coder states end here.
pub const FINAL: u64 = 0xFFFF;

/// Why a block was refused. `Data` texts are gitfurl's own and match the Python decoder's.
#[derive(Debug)]
pub enum RolzError {
    Data(Cow<'static, str>),
    /// The data source failed or asked to stop (the caller holds the reason).
    External,
    /// A buffer the plan allows could not be had.
    Memory,
}

impl fmt::Display for RolzError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            RolzError::Data(s) => f.write_str(s),
            RolzError::External => f.write_str("the data could not be read"),
            RolzError::Memory => f.write_str("not enough memory for the rolz stage"),
        }
    }
}

impl std::error::Error for RolzError {}

#[inline]
pub(crate) fn bad(s: &'static str) -> RolzError {
    RolzError::Data(Cow::Borrowed(s))
}

/// Where the block's bytes come from.
pub trait Source {
    /// Exactly `n` bytes at offset `off` of the block.
    fn read(&mut self, off: u64, n: usize) -> Result<Vec<u8>, RolzError>;
    /// Called every few thousand tokens and before each x86 request: an error ends the
    /// decode (Stop).
    fn stop(&mut self) -> Result<(), RolzError> {
        Ok(())
    }
}

/// A byte slice as the block.
pub struct SliceSource<'a>(pub &'a [u8]);

impl Source for SliceSource<'_> {
    fn read(&mut self, off: u64, n: usize) -> Result<Vec<u8>, RolzError> {
        usize::try_from(off)
            .ok()
            .and_then(|a| self.0.get(a..a.checked_add(n)?))
            .map(|s| s.to_vec())
            .ok_or_else(|| bad("damaged data (a read past the end of the block)"))
    }
}

/// A vector of `n` zeros, or `RolzError::Memory`.
pub(crate) fn zeroed<T: Clone + Default>(n: usize) -> Result<Vec<T>, RolzError> {
    let mut v = Vec::new();
    v.try_reserve_exact(n).map_err(|_| RolzError::Memory)?;
    v.resize(n, T::default());
    Ok(v)
}

// --- tables from the specification (as in farc_rolz.py) ------------------------------------

/// Bits and bases of one bucket table (bases wrap at 32 bits, as the specification's do).
pub struct Buckets {
    pub bits: Vec<u32>,
    pub base: Vec<u64>,
}

fn buckets(nbits: &[u32]) -> Buckets {
    let mut base = Vec::with_capacity(nbits.len() + 1);
    let mut b = 0u64;
    for &n in nbits {
        base.push(b);
        b = (b + (1u64 << n)) & M32;
    }
    base.push(b);
    Buckets {
        bits: nbits.to_vec(),
        base,
    }
}

pub struct Tables {
    pub ta: Buckets, // rep length
    pub tb: Buckets, // match length
    pub tc: Buckets, // reduced-offset length
    pub td: Buckets, // match distance bucket
    pub te: Buckets, // reduced-offset index
    pub tg: Buckets, // byte deltas
    pub ti: Buckets, // word deltas
    pub tk: Buckets, // dword deltas
    pub tl: Buckets, // pixel channel 0
    pub tm: Buckets, // pixel channels 1, 2
    pub tn: Buckets, // residuals of specials 9-13
    pub litcls: [u8; 256],
    pub st: &'static States,
}

impl Tables {
    pub fn tc_bias(&self) -> i64 {
        self.tc.base[10] as i64
    }
    pub fn tk_bias(&self) -> i64 {
        self.tk.base[15] as i64
    }
    pub fn tl_bias(&self) -> i64 {
        self.tl.base[8] as i64
    }
    pub fn tm_bias(&self) -> i64 {
        self.tm.base[8] as i64
    }
}

const LITRANGES: [(u8, u8, u8); 30] = [
    (0x00, 0x0F, 0),
    (0x10, 0x20, 1),
    (0x21, 0x2F, 2),
    (0x30, 0x39, 3),
    (0x3A, 0x40, 2),
    (0x41, 0x41, 4),
    (0x42, 0x44, 5),
    (0x45, 0x45, 4),
    (0x46, 0x48, 5),
    (0x49, 0x49, 4),
    (0x4A, 0x4E, 5),
    (0x4F, 0x4F, 4),
    (0x50, 0x54, 5),
    (0x55, 0x55, 4),
    (0x56, 0x5A, 5),
    (0x5B, 0x60, 6),
    (0x61, 0x61, 7),
    (0x62, 0x64, 8),
    (0x65, 0x65, 7),
    (0x66, 0x68, 8),
    (0x69, 0x69, 7),
    (0x6A, 0x6E, 8),
    (0x6F, 0x6F, 7),
    (0x70, 0x74, 8),
    (0x75, 0x75, 7),
    (0x76, 0x7A, 8),
    (0x7B, 0x9F, 9),
    (0xA0, 0xBF, 10),
    (0xC0, 0xDF, 11),
    (0xE0, 0xFF, 12),
];

fn make_tables() -> Tables {
    let mut tk = vec![31, 28];
    tk.extend((2..=26).rev().step_by(2));
    tk.push(0);
    tk.extend((2..=26).step_by(2));
    tk.extend([28, 31]);
    let mut ti = vec![0, 0];
    ti.extend(1..15);
    ti.extend((1..=14).rev());
    ti.extend([0, 0]);
    let mut tn = vec![0; 5];
    tn.extend(1..16);
    let mut litcls = [0u8; 256];
    for &(lo, hi, cls) in &LITRANGES {
        for c in lo..=hi {
            litcls[c as usize] = cls;
        }
    }
    let mut ta = vec![0; 9];
    ta.extend([1, 2, 3, 4, 5, 6, 0]);
    let mut tb = vec![0; 10];
    tb.extend([1, 2, 3, 4, 5, 6]);
    let mut tc = vec![6, 5, 4, 3, 2, 2, 2, 1];
    tc.extend([0; 6]);
    tc.extend([1, 1, 2, 2, 3, 4, 5, 6]);
    Tables {
        ta: buckets(&ta),
        tb: buckets(&tb),
        tc: buckets(&tc),
        td: buckets(&(8..30).collect::<Vec<_>>()),
        te: buckets(&(0..27).collect::<Vec<_>>()),
        tg: buckets(&[0, 0, 1, 2, 3, 4, 5, 6, 6, 5, 4, 3, 2, 1, 0, 0]),
        ti: buckets(&ti),
        tk: buckets(&tk),
        tl: buckets(&[7, 6, 5, 4, 3, 2, 1, 0, 0, 0, 1, 2, 3, 4, 5, 6, 7]),
        tm: buckets(&[8, 7, 6, 5, 4, 3, 2, 1, 0, 1, 2, 3, 4, 5, 6, 7, 8]),
        tn: buckets(&tn),
        litcls,
        st: states(),
    }
}

pub fn tables() -> &'static Tables {
    static T: OnceLock<Tables> = OnceLock::new();
    T.get_or_init(make_tables)
}

// --- CRC-32 (zlib's) ----------------------------------------------------------------------------

fn crc_table() -> &'static [[u32; 256]; 4] {
    static T: OnceLock<[[u32; 256]; 4]> = OnceLock::new();
    T.get_or_init(|| {
        let mut t = [[0u32; 256]; 4];
        for i in 0..256 {
            let mut c = i as u32;
            for _ in 0..8 {
                c = if c & 1 != 0 {
                    0xEDB8_8320 ^ (c >> 1)
                } else {
                    c >> 1
                };
            }
            t[0][i] = c;
        }
        for i in 0..256 {
            for k in 1..4 {
                let p = t[k - 1][i];
                t[k][i] = (p >> 8) ^ t[0][(p & 0xFF) as usize];
            }
        }
        t
    })
}

/// zlib.crc32(data, crc).
pub fn crc32(crc: u32, data: &[u8]) -> u32 {
    let t = crc_table();
    let mut c = !crc;
    let (quads, rest) = data.as_chunks::<4>();
    for q in quads {
        c ^= u32::from_le_bytes(*q);
        c = t[3][(c & 0xFF) as usize]
            ^ t[2][((c >> 8) & 0xFF) as usize]
            ^ t[1][((c >> 16) & 0xFF) as usize]
            ^ t[0][(c >> 24) as usize];
    }
    for &b in rest {
        c = t[0][((c ^ b as u32) & 0xFF) as usize] ^ (c >> 8);
    }
    !c
}

/// `n` with a comma between groups of three digits (Python's "{:,}").
pub fn grouped(n: u64) -> String {
    let s = n.to_string();
    let mut out = String::new();
    for (i, ch) in s.chars().enumerate() {
        if i > 0 && (s.len() - i).is_multiple_of(3) {
            out.push(',');
        }
        out.push(ch);
    }
    out
}

/// Bytes one record may hold at most, for a window of `window` bytes (as
/// farc_rolz.memory_for(); the plan counts this).
pub fn memory_for(window: u64) -> u64 {
    let pages = window / 0x1400 + 0x201;
    let mids = window / 0x280000 + 0x201;
    let tables = 6 * 512 * pages + 2048 * mids + 256 * 512 * 4 + 4 * (pages + mids);
    let small = 2 * (SMALL_WINDOW + HIST as u64 + TRIM_SLACK as u64 + 2 * REQUEST);
    2 * (window + HIST as u64 + 1024)
        + tables
        + small
        + 5 * (((1 << 24) - 1) + 7)
        + 4 * REQUEST
        + (24 << 20)
}

// --- the directory ------------------------------------------------------------------------------

/// The decoded directory: the window, and the one regular file's size and CRC.
pub struct Directory {
    pub window: u64,
    pub size: u64,
    pub crc: u32,
}

fn u32_at(b: &[u8], at: usize) -> u64 {
    match b.get(at..at + 4) {
        Some(q) => u32::from_le_bytes([q[0], q[1], q[2], q[3]]) as u64,
        None => 0,
    }
}

fn u64_at(b: &[u8], at: usize) -> u64 {
    match b.get(at..at + 8) {
        Some(q) => u64::from_le_bytes(q.try_into().unwrap_or([0; 8])),
        None => 0,
    }
}

impl Directory {
    fn read(lz: &mut Lz, src: &mut dyn Source) -> Result<Directory, RolzError> {
        let head = lz.take(src, 8)?;
        let window = u32_at(&head, 0);
        let count = u32_at(&head, 4) as usize;
        if !(1..=(DIR_CAP - 8) / 29).contains(&count) {
            return Err(bad(
                "damaged directory (a file count gitfurl does not accept)",
            ));
        }
        let mut names = 0;
        let mut used = 8;
        while names < count {
            if used >= DIR_CAP {
                return Err(bad("damaged directory (longer than gitfurl reads)"));
            }
            used += 1;
            if lz.take(src, 1)? == [0] {
                names += 1;
            }
        }
        let rest = lz.take(src, 28 * count)?;
        let at_crc = 16 * count;
        let at_attr = at_crc + 4 * count;
        let mut file = None;
        let mut files = 0;
        let mut folder_size = false;
        for i in 0..count {
            let size = u64_at(&rest, 8 * i);
            if u32_at(&rest, at_attr + 4 * i) & 0x10 == 0 {
                files += 1;
                file = Some((size, u32_at(&rest, at_crc + 4 * i) as u32));
            } else if size != 0 {
                folder_size = true;
            }
        }
        let (size, crc) = match file {
            Some(f) if files == 1 && !folder_size => f,
            _ => return Err(bad("a directory that does not hold exactly one file")),
        };
        if window == 0 || size > window {
            return Err(bad("damaged directory (a file larger than its window)"));
        }
        Ok(Directory { window, size, crc })
    }
}

// --- records and blocks -------------------------------------------------------------------------

/// Sizes a record's decode reached (for checks against real data).
#[derive(Clone, Copy, Debug, Default)]
pub struct RecordInfo {
    pub window: u64,
    pub size: u64,
    pub crc: u32,
    pub main: u64,
    pub tables: u64,
}

enum Phase {
    Start,
    Next,
    Fill,
    Copy,
    Tail,
    Rest,
    Checks,
    Done,
}

struct Pipe {
    cd: Coder, // stream 1: the long-range copies (streams 2, 3 and 4 are in the LZs and calls)
    main: Lz,
    tlz: Lz,
    calls: X86Calls,
    g: [[u8; 64]; 3],
    total: u64,
    count: u64,
    left: u64,
    shift_base: u32,
    crc_want: u32,
}

/// One record: its pipeline, run a step at a time so that pieces come out as they are made.
struct Record {
    base: u64,
    size: u64,
    budget: u64,
    chunk: usize,
    phase: Phase,
    pipe: Option<Box<Pipe>>,
    out: Vec<u8>,
    sent: usize,
    crc: u32,
    pending: Vec<u8>,
    pend_at: usize,
    pos: u64,
    fill_to: u64,
    copy_dist: usize,
    copy_len: u128,
    info: RecordInfo,
}

impl Record {
    fn new(base: u64, size: u64, budget: u64, chunk: usize) -> Record {
        Record {
            base,
            size,
            budget,
            chunk,
            phase: Phase::Start,
            pipe: None,
            out: Vec::new(),
            sent: 0,
            crc: 0,
            pending: Vec::new(),
            pend_at: 0,
            pos: 0,
            fill_to: 0,
            copy_dist: 0,
            copy_len: 0,
            info: RecordInfo::default(),
        }
    }

    fn start(&mut self, src: &mut dyn Source) -> Result<(), RolzError> {
        let t = tables();
        let box_ = Container::read(src, self.base, self.size)?;
        let [c0, c1, c2, c3, c4] = box_.chunks;
        let mk = |c| Coder::new(self.base, c);
        let mut dlz = Lz::new(mk(c0), SMALL_WINDOW, false, true)?;
        let d = Directory::read(&mut dlz, src)?;
        if dlz.pending() != 0 || !dlz.cd.used_up() {
            return Err(bad("damaged directory (its stream is not used up exactly)"));
        }
        drop(dlz); // its stream stays used up: nothing reads it again
        if d.window > self.budget {
            return Err(RolzError::Data(Cow::Owned(format!(
                "a window of {} bytes, over the {} planned",
                grouped(d.window),
                grouped(self.budget)
            ))));
        }
        self.info.window = d.window;
        self.info.size = d.size;
        self.info.crc = d.crc;
        let total = d.size;
        let main = Lz::new(mk(c4), d.window, true, false)?;
        let tlz = Lz::new(mk(c2), SMALL_WINDOW, false, true)?;
        let calls = X86Calls::new(mk(c3));
        let mut g = [[128u8; 64]; 3];
        let mut cd = mk(c1);
        let count = cd.gamma(src, t.st, &mut g[0])?;
        let left = cd.gamma(src, t.st, &mut g[0])?; // new bytes still to come from the calls
        if left > total || count > total >> 8 {
            return Err(bad("damaged data (copy counts larger than the file)"));
        }
        let bits = 64 - d.window.leading_zeros();
        let shift_base = (bits as i64 - 1 - 5).max(16) as u32;
        self.out = Vec::new();
        self.out
            .try_reserve_exact(total as usize)
            .map_err(|_| RolzError::Memory)?;
        self.pipe = Some(Box::new(Pipe {
            cd,
            main,
            tlz,
            calls,
            g,
            total,
            count,
            left,
            shift_base,
            crc_want: d.crc,
        }));
        Ok(())
    }

    fn emit(&mut self, n: usize) -> Vec<u8> {
        let piece = self.out[self.sent..self.sent + n].to_vec();
        self.crc = crc32(self.crc, &piece);
        self.sent += n;
        piece
    }

    /// One step of filling from the call stage, up to `upto` bytes of output.
    fn fill_step(&mut self, src: &mut dyn Source, upto: u64) -> Result<(), RolzError> {
        let Some(p) = self.pipe.as_deref_mut() else {
            return Err(bad("damaged data (a record out of order)"));
        };
        if self.pend_at == self.pending.len() {
            if p.left == 0 {
                return Err(bad(
                    "damaged data (the file needs more new bytes than there are)",
                ));
            }
            let n = p.left.min(REQUEST);
            p.left -= n;
            src.stop()?;
            self.pending = p.calls.request(src, &mut p.tlz, &mut p.main, n as usize)?;
            self.pend_at = 0;
        }
        let k = ((upto - self.out.len() as u64) as usize)
            .min(self.pending.len() - self.pend_at)
            .min(self.chunk);
        self.out
            .extend_from_slice(&self.pending[self.pend_at..self.pend_at + k]);
        self.pend_at += k;
        Ok(())
    }

    /// The next piece of the record's file, or None after its end checks.
    fn next(&mut self, src: &mut dyn Source) -> Result<Option<Vec<u8>>, RolzError> {
        let t = tables();
        loop {
            if self.out.len() - self.sent >= self.chunk {
                return Ok(Some(self.emit(self.chunk)));
            }
            match self.phase {
                Phase::Start => {
                    self.start(src)?;
                    self.phase = Phase::Next;
                }
                Phase::Next => {
                    let Some(p) = self.pipe.as_deref_mut() else {
                        return Ok(None);
                    };
                    if p.count == 0 {
                        self.phase = Phase::Tail;
                        continue;
                    }
                    p.count -= 1;
                    let cd = &mut p.cd;
                    let step = cd.gamma(src, t.st, &mut p.g[0])?;
                    let pos = match self.pos.checked_add(step) {
                        Some(x) if x <= p.total => x,
                        _ => return Err(bad("damaged data (a copy past the end of the file)")),
                    };
                    let s = if pos >> (p.shift_base + 8) == 0 {
                        8
                    } else {
                        (64 - pos.leading_zeros() - p.shift_base).min(16)
                    };
                    let back = cd.gamma(src, t.st, &mut p.g[1])?;
                    let length = (cd.gamma(src, t.st, &mut p.g[2])? as u128 + 1) << s;
                    let from = (((pos >> s) as i128) - back as i128) << s;
                    if !(0 <= from && from < pos as i128) || length > (p.total - pos) as u128 {
                        return Err(bad("damaged data (a copy outside the file)"));
                    }
                    self.pos = pos;
                    self.fill_to = pos;
                    self.copy_dist = (pos as i128 - from) as usize;
                    self.copy_len = length;
                    self.phase = Phase::Fill;
                }
                Phase::Fill => {
                    if (self.out.len() as u64) < self.fill_to {
                        self.fill_step(src, self.fill_to)?;
                    } else {
                        self.phase = Phase::Copy;
                    }
                }
                Phase::Copy => {
                    if self.copy_len > 0 {
                        let k =
                            (self.copy_len.min(self.chunk as u128) as usize).min(self.copy_dist);
                        let at = self.out.len() - self.copy_dist;
                        self.out.extend_from_within(at..at + k);
                        self.copy_len -= k as u128;
                    } else {
                        self.pos = self.out.len() as u64;
                        self.phase = Phase::Next;
                    }
                }
                Phase::Tail => {
                    let Some(total) = self.pipe.as_deref().map(|p| p.total) else {
                        return Ok(None);
                    };
                    if (self.out.len() as u64) < total {
                        self.fill_step(src, total)?;
                    } else {
                        let left = self.pipe.as_deref().map_or(0, |p| p.left);
                        if self.pend_at < self.pending.len() || left != 0 {
                            return Err(bad("damaged data (more new bytes than the file holds)"));
                        }
                        self.phase = Phase::Rest;
                    }
                }
                Phase::Rest => {
                    self.phase = Phase::Checks;
                    if self.sent < self.out.len() {
                        return Ok(Some(self.emit(self.out.len() - self.sent)));
                    }
                }
                Phase::Checks => {
                    self.phase = Phase::Done;
                    self.out = Vec::new(); // the file is no longer needed for copies
                    self.sent = 0;
                    let Some(p) = self.pipe.take() else {
                        return Ok(None);
                    };
                    if self.crc != p.crc_want {
                        return Err(bad("the file's CRC does not match"));
                    }
                    if p.main.pending() != 0 || p.tlz.pending() != 0 {
                        return Err(bad("damaged data (an LZ gave more than was read from it)"));
                    }
                    let streams = [&p.cd, &p.tlz.cd, &p.calls.cd, &p.main.cd];
                    if !streams.iter().all(|c| c.used_up()) {
                        return Err(bad("damaged data (a stream is not used up exactly)"));
                    }
                    self.info.main = p.main.given();
                    self.info.tables = p.tlz.given();
                    return Ok(None);
                }
                Phase::Done => return Ok(None),
            }
        }
    }
}

/// A block's output: its records back to back, until its input is used up, as pieces of at
/// most `chunk` bytes. `budget` is the largest window the plan allows; with a `limit`, more
/// output than that is refused. After an error every later call gives None.
pub struct Block {
    size: u64,
    budget: u64,
    chunk: usize,
    limit: Option<u64>,
    at: u64,
    rec_total: u64,
    given: u64,
    rec: Option<Box<Record>>,
    failed: bool,
    pub last: Option<RecordInfo>,
}

impl Block {
    pub fn new(size: u64, budget: u64, chunk: usize, limit: Option<u64>) -> Block {
        Block {
            size,
            budget,
            chunk: chunk.max(1),
            limit,
            at: 0,
            rec_total: 0,
            given: 0,
            rec: None,
            failed: false,
            last: None,
        }
    }

    pub fn next_piece(&mut self, src: &mut dyn Source) -> Result<Option<Vec<u8>>, RolzError> {
        if self.failed {
            return Ok(None);
        }
        let mut src = Bounded {
            src,
            size: self.size,
        };
        let r = self.step(&mut src);
        if r.is_err() {
            self.failed = true;
            self.rec = None;
        }
        r
    }

    fn step(&mut self, src: &mut dyn Source) -> Result<Option<Vec<u8>>, RolzError> {
        loop {
            if self.rec.is_none() {
                if self.at >= self.size {
                    return Ok(None);
                }
                if self.size - self.at < 4 {
                    return Err(bad("damaged data (a record header is cut off)"));
                }
                let total = u32_at(&src.read(self.at, 4)?, 0);
                if !(4 < total && total <= self.size - self.at) {
                    return Err(bad(
                        "damaged data (a record length is too small or runs past the block)",
                    ));
                }
                self.rec_total = total;
                self.rec = Some(Box::new(Record::new(
                    self.at + 4,
                    total - 4,
                    self.budget,
                    self.chunk,
                )));
            }
            let Some(rec) = self.rec.as_deref_mut() else {
                continue;
            };
            match rec.next(src)? {
                Some(piece) => {
                    self.given += piece.len() as u64;
                    if self.limit.is_some_and(|l| self.given > l) {
                        return Err(bad("more data than the block's files hold"));
                    }
                    return Ok(Some(piece));
                }
                None => {
                    self.last = Some(rec.info);
                    self.rec = None;
                    self.at += self.rec_total;
                }
            }
        }
    }
}

/// Reads checked against the block's size, as the Python stage's reader does.
struct Bounded<'a> {
    src: &'a mut dyn Source,
    size: u64,
}

impl Source for Bounded<'_> {
    fn read(&mut self, off: u64, n: usize) -> Result<Vec<u8>, RolzError> {
        match off.checked_add(n as u64) {
            Some(end) if end <= self.size => self.src.read(off, n),
            _ => Err(bad("damaged data (a read past the end of the block)")),
        }
    }
    fn stop(&mut self) -> Result<(), RolzError> {
        self.src.stop()
    }
}

/// The whole output of a block held in memory (for tests and tools).
pub fn decode(
    data: &[u8],
    budget: u64,
    chunk: usize,
    limit: Option<u64>,
) -> Result<Vec<u8>, RolzError> {
    let mut b = Block::new(data.len() as u64, budget, chunk, limit);
    let mut src = SliceSource(data);
    let mut out = Vec::new();
    while let Some(p) = b.next_piece(&mut src)? {
        out.extend_from_slice(&p);
    }
    Ok(out)
}
