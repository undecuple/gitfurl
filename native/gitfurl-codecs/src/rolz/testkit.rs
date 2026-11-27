//! Test-only stream builders (as tests/rolz_writer.py): recorded coder operations, a plain
//! two-state rANS encoder for them, packets, containers and records. `Rec` replays the
//! decoder's own models for the few token kinds the tests write (literals in state 0, gamma
//! codes and model bits), so the streams it makes are what the real reader must decode.
#![forbid(unsafe_code)]

use super::coder::{states, Cdf};
use super::{crc32, FINAL, MAGIC, REQUEST};

#[derive(Clone, Copy, Debug)]
pub enum Op {
    Bit { p: u32, b: u32 },
    Raw { n: u32, v: u64 },
    Sym { start: u32, f: u32 },
}

fn encode_one(op: Op, x: u64) -> u64 {
    match op {
        Op::Bit { p, b } => {
            let p = p as u64;
            if b != 0 {
                ((x / p) << 12) + x % p
            } else {
                let f = 4096 - p;
                ((x / f) << 12) + p + x % f
            }
        }
        Op::Raw { n, v } => x << n | v,
        Op::Sym { start, f } => {
            let f = f as u64;
            ((x / f) << 14) + start as u64 + x % f
        }
    }
}

/// Largest state + 1 that op can be encoded from without passing 32 bits.
fn bound(op: Op) -> u64 {
    match op {
        Op::Bit { p, b } => (if b != 0 { p } else { 4096 - p } as u64) << 20,
        Op::Raw { n, .. } => 1 << (32 - n),
        Op::Sym { f, .. } => (f as u64) << 18,
    }
}

/// One packet payload that the reader decodes as `ops`, ending with both states at FINAL.
pub fn encode(ops: &[Op]) -> Vec<u8> {
    let mut state = [FINAL, FINAL];
    let mut words = Vec::new();
    for i in (0..ops.len()).rev() {
        let mut x = encode_one(ops[i], state[i & 1]);
        assert!(x < 1 << 32);
        if i >= 1 {
            let limit = if i >= 2 { bound(ops[i - 2]) } else { 1 << 32 };
            if x < 1 << 16 || x >= limit {
                words.push((x & 0xFFFF) as u16);
                x >>= 16;
            }
        }
        state[i & 1] = x;
    }
    let [s0, s1] = state;
    words.extend([
        (s1 & 0xFFFF) as u16,
        (s1 >> 16) as u16,
        (s0 & 0xFFFF) as u16,
        (s0 >> 16) as u16,
    ]);
    words.iter().flat_map(|w| w.to_le_bytes()).collect()
}

pub fn packet(payload: &[u8]) -> Vec<u8> {
    let head = (payload.len() as u32).to_le_bytes();
    let crc = crc32(crc32(0, &head[..3]), payload);
    let mut out = head[..3].to_vec();
    out.extend(crc.to_le_bytes());
    out.extend_from_slice(payload);
    out
}

pub fn varint(v: u64) -> Vec<u8> {
    if v < 128 {
        return vec![(v << 1) as u8];
    }
    let b = ((2 * v + 1) & 0xFF) as u8;
    let mut out = vec![b];
    out.extend(varint((2 * v + 1) >> 8));
    out
}

/// A container with these (type, chunk bytes) in file order.
pub fn container(chunks: &[(u64, Vec<u8>)]) -> Vec<u8> {
    let body: Vec<u8> = chunks.iter().flat_map(|(_, c)| c.clone()).collect();
    let index_at = 4 + 13 + body.len() as u64;
    let mut out = MAGIC.to_vec();
    out.extend(packet(&index_at.to_le_bytes()[..6]));
    out.extend(&body);
    let mut last = [0i64; 8];
    let mut idx = varint(chunks.len() as u64);
    for (t, c) in chunks.iter().rev() {
        let d = c.len() as i64 - last[*t as usize];
        last[*t as usize] = c.len() as i64;
        let v = if d < 0 { 8 | ((!d) << 4) } else { d << 4 };
        idx.extend(varint(*t | v as u64));
    }
    for part in idx.chunks(4096) {
        out.extend(packet(part));
    }
    out
}

pub fn record(archive: &[u8]) -> Vec<u8> {
    let mut out = ((archive.len() + 4) as u32).to_le_bytes().to_vec();
    out.extend_from_slice(archive);
    out
}

/// Records the operations of the token kinds the tests write, with the decoder's models.
pub struct Rec {
    pub ops: Vec<Op>,
    st1: Vec<u8>,
    st2: Vec<u8>,
    apm: [u32; 64],
    lit: Vec<Cdf>,
    pos: usize,
    prev: u8,
}

impl Rec {
    pub fn new() -> Rec {
        let mut apm = [0u32; 64];
        for r in 0..8 {
            for c in 0..8 {
                apm[r * 8 + c] = ((r + c + 1) * 0x1000) as u32;
            }
        }
        Rec {
            ops: Vec::new(),
            st1: vec![128; 1024],
            st2: vec![128; 224],
            apm,
            lit: vec![Cdf::uniform(16); 13 * 81],
            pos: 0,
            prev: 0,
        }
    }

    fn sym(&mut self, i: usize, s: usize) {
        let c = &mut self.lit[i];
        self.ops.push(Op::Sym {
            start: c.v[s] as u32,
            f: (c.v[s + 1] - c.v[s]) as u32,
        });
        c.update(s);
    }

    /// A literal token from LZ state 0 (literals keep it).
    pub fn literal(&mut self, b: u8) {
        let st = states();
        let t = super::tables();
        let i1 = self.prev as usize * 4;
        let i2 = (self.pos & 3) * 4;
        let (a, s2) = (self.st1[i1] as usize, self.st2[i2] as usize);
        let ci = st.qs[a] as usize * 8 + st.qs[s2] as usize;
        let p = self.apm[ci];
        self.ops.push(Op::Bit { p: p >> 4, b: 1 });
        self.apm[ci] = p + ((65536 - p) >> 7);
        self.st1[i1] = st.next1[a];
        self.st2[i2] = st.next1[s2];
        let k = t.litcls[self.prev as usize] as usize * 81;
        let hi = (b >> 4) as usize;
        self.sym(k, hi);
        self.sym(k + 1 + hi, (b & 15) as usize);
        self.prev = b;
        self.pos += 1;
    }

    pub fn model_bit(&mut self, models: &mut [u8], i: usize, b: u32) {
        let st = states();
        let s = models[i] as usize;
        self.ops.push(Op::Bit { p: st.p12[s], b });
        models[i] = if b != 0 { st.next1[s] } else { st.next0[s] };
    }

    pub fn gamma(&mut self, models: &mut [u8; 64], v: u64) {
        let n = 63 - (v + 1).leading_zeros();
        let mut node = 1usize;
        for i in 0..6 {
            let b = (n >> (5 - i)) & 1;
            self.model_bit(models, node, b);
            node = node << 1 | b as usize;
        }
        let rest = (v + 1) - (1u64 << n);
        // raw(n) reads its high part first, 16 bits at a time from the low end
        let mut widths = Vec::new();
        let mut m = n;
        while m > 16 {
            widths.insert(0, 16);
            m -= 16;
        }
        widths.insert(0, m);
        let mut shift = n;
        for w in widths {
            shift -= w;
            if w != 0 {
                self.ops.push(Op::Raw {
                    n: w,
                    v: (rest >> shift) & ((1 << w) - 1),
                });
            }
        }
    }
}

pub fn literals(data: &[u8]) -> Vec<Op> {
    let mut r = Rec::new();
    for &b in data {
        r.literal(b);
    }
    r.ops
}

pub fn directory(window: u32, size: u64, crc: u32) -> Vec<u8> {
    let mut out = window.to_le_bytes().to_vec();
    out.extend(2u32.to_le_bytes());
    out.extend(b"tmp\0f1\0");
    out.extend(0u64.to_le_bytes());
    out.extend(size.to_le_bytes());
    out.extend([0u8; 16]);
    out.extend(0u32.to_le_bytes());
    out.extend(crc.to_le_bytes());
    out.extend(0x10u32.to_le_bytes());
    out.extend(0x20u32.to_le_bytes());
    out.extend([0u8; 8]);
    out
}

/// A whole record whose file is `data`, written as literals only, with no long-range copies
/// and every x86 request passed on.
pub fn stored_file(data: &[u8], window: u32) -> Vec<u8> {
    let d = directory(window, data.len() as u64, crc32(0, data));
    let requests = data.len().div_ceil(REQUEST as usize);
    let mut fl = Rec::new();
    let mut flag = [128u8];
    for _ in 0..requests {
        fl.model_bit(&mut flag, 0, 0);
    }
    let mut s1 = Rec::new();
    let mut g0 = [128u8; 64];
    s1.gamma(&mut g0, 0);
    s1.gamma(&mut g0, data.len() as u64);
    let mut chunks = Vec::new();
    if !data.is_empty() {
        chunks.push((4, packet(&encode(&literals(data)))));
        chunks.push((3, packet(&encode(&fl.ops))));
    }
    chunks.push((1, packet(&encode(&s1.ops))));
    chunks.push((0, packet(&encode(&literals(&d)))));
    record(&container(&chunks))
}
