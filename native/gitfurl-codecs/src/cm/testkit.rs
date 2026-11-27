//! Test-only stream builder: a coder that chooses items (from a script, or at random among
//! those the models allow) and records their intervals, and a byte-wise rANS encoder for
//! those intervals. Running the decoder's own model code with this coder and encoding what it
//! recorded gives a stream the real decoder must decode to the same bytes, ending with the
//! state the end check expects.
#![forbid(unsafe_code)]

use std::collections::VecDeque;

use super::rans::{update_bin, Coder, RANS_LOW};

pub struct TestCoder {
    pub rec: Vec<(u32, u32, u32)>, // (start, frequency, precision bits)
    pub script: VecDeque<u32>,
    random: bool,
    seed: u64,
}

impl TestCoder {
    /// Takes every item from `script` (and panics when it runs out).
    pub fn scripted(script: &[u32]) -> Self {
        TestCoder {
            rec: Vec::new(),
            script: script.iter().copied().collect(),
            random: false,
            seed: 0,
        }
    }
    /// Takes items from the script while it lasts, then at random.
    pub fn random(seed: u64) -> Self {
        TestCoder {
            rec: Vec::new(),
            script: VecDeque::new(),
            random: true,
            seed: seed | 1,
        }
    }
    pub fn rand(&mut self) -> u64 {
        // xorshift64*
        self.seed ^= self.seed >> 12;
        self.seed ^= self.seed << 25;
        self.seed ^= self.seed >> 27;
        self.seed.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }
    fn next(&mut self) -> Option<u32> {
        match self.script.pop_front() {
            Some(v) => Some(v),
            None if self.random => None,
            None => panic!("the test script ran out"),
        }
    }
    /// Codes bit `v` with probability `p` and updates p (a decision the test makes itself).
    pub fn force_bit(&mut self, p: &mut u16, bits: u32, sh: u32, v: usize) {
        self.record_bit(*p as u32, bits, v);
        update_bin(p, v, sh, bits);
    }
    fn record_bit(&mut self, p: u32, bits: u32, v: usize) {
        let one = 1u32 << bits;
        self.rec.push(if v == 0 {
            (0, p, bits)
        } else {
            (p, one - p, bits)
        });
    }
}

impl Coder for TestCoder {
    fn pick(&mut self, c: &[u16], n: usize) -> usize {
        let s = match self.next() {
            Some(v) => v as usize,
            None => loop {
                let s = (self.rand() % n as u64) as usize;
                if c[s + 1] > c[s] {
                    break s;
                }
            },
        };
        assert!(s < n, "scripted symbol {s} out of range {n}");
        let f = c[s + 1] as u32 - c[s] as u32;
        assert!(f > 0, "scripted symbol {s} has no room");
        self.rec.push((c[s] as u32, f, 15));
        s
    }
    fn pick_bit(&mut self, p: u32, bits: u32) -> usize {
        let v = match self.next() {
            Some(v) => v as usize,
            None => (self.rand() & 1) as usize,
        };
        self.record_bit(p, bits, v);
        v
    }
    fn raw(&mut self, k: u32) -> u32 {
        let v = match self.next() {
            Some(v) => v,
            None => self.rand() as u32,
        } & ((1u32 << k) - 1);
        self.rec.push((v, 1, k));
        v
    }
}

/// Encodes recorded intervals: the stream starts with the final state (4 bytes LE), and the
/// decoder ends with its state at RANS_LOW after the last item.
pub fn encode(rec: &[(u32, u32, u32)]) -> Vec<u8> {
    let mut x: u64 = RANS_LOW as u64;
    let mut emitted = Vec::new();
    for &(start, f, bits) in rec.iter().rev() {
        let f = f as u64;
        let xmax = (((RANS_LOW >> bits) as u64) << 8) * f;
        while x >= xmax {
            emitted.push(x as u8);
            x >>= 8;
        }
        x = ((x / f) << bits) + (x % f) + start as u64;
    }
    let mut out = (x as u32).to_le_bytes().to_vec();
    emitted.reverse();
    out.extend_from_slice(&emitted);
    out
}

/// The script for one value of the stream-A number model: 0, or 1 followed by its bit count
/// and its bits below the top one.
pub fn number(v: u64) -> Vec<u32> {
    if v == 0 {
        return vec![0];
    }
    let c = 63 - v.leading_zeros();
    let mut s = vec![1];
    if c >= 15 {
        s.push(15);
        s.push(c - 15);
    } else {
        s.push(c);
    }
    for i in (0..c).rev() {
        s.push(((v >> i) & 1) as u32);
    }
    s
}
