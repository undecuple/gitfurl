//! Sequential reads of one stream (a job's stream A or stream B), a chunk at a time, from
//! whatever holds the compressed data (a byte slice, or the archive through Python).
#![forbid(unsafe_code)]

use super::CmError;

/// Bytes fetched at once.
pub const CHUNK: usize = 1 << 20;

/// Where a job's compressed bytes come from.
pub trait Fetch {
    /// Appends exactly `n` bytes, from offset `off` of the compressed data, to `buf`.
    fn fetch(&mut self, off: u64, n: usize, buf: &mut Vec<u8>) -> Result<(), CmError>;
    /// Called between segments and between chunks: an error here ends the decode (Stop).
    fn check(&mut self) -> Result<(), CmError> {
        Ok(())
    }
}

/// A byte slice as the compressed data.
pub struct SliceFetch<'a>(pub &'a [u8]);

impl Fetch for SliceFetch<'_> {
    fn fetch(&mut self, off: u64, n: usize, buf: &mut Vec<u8>) -> Result<(), CmError> {
        let start = usize::try_from(off).map_err(|_| CmError::Corrupt("a read past the end"))?;
        let end = start
            .checked_add(n)
            .ok_or(CmError::Corrupt("a read past the end"))?;
        let part = self
            .0
            .get(start..end)
            .ok_or(CmError::Corrupt("a read past the end"))?;
        buf.extend_from_slice(part);
        Ok(())
    }
}

/// One stream of `size` bytes at offset `base`, read in order. `limit` is how many bytes
/// the current reader (one coded segment, or stream A) may still take; reading past it, or
/// past the stream, gives zero bytes and marks the stream as damaged.
pub struct Input<'f> {
    buf: Vec<u8>,
    p: usize,
    next_off: u64,
    unfetched: u64,
    pub limit: u64,
    pub overrun: bool,
    pub err: Option<CmError>,
    fetch: &'f mut dyn Fetch,
}

impl<'f> Input<'f> {
    pub fn new(fetch: &'f mut dyn Fetch, base: u64, size: u64) -> Self {
        Input {
            buf: Vec::new(),
            p: 0,
            next_off: base,
            unfetched: size,
            limit: size,
            overrun: false,
            err: None,
            fetch,
        }
    }

    /// Bytes not read yet.
    pub fn remaining(&self) -> u64 {
        self.unfetched + (self.buf.len() - self.p) as u64
    }

    #[inline]
    pub fn byte(&mut self) -> u8 {
        if self.limit == 0 || (self.p == self.buf.len() && !self.refill()) {
            self.overrun = true;
            return 0;
        }
        self.limit -= 1;
        let b = self.buf[self.p];
        self.p += 1;
        b
    }

    #[cold]
    fn refill(&mut self) -> bool {
        if self.unfetched == 0 || self.err.is_some() {
            return false;
        }
        let n = (self.unfetched.min(CHUNK as u64)) as usize;
        self.buf.clear();
        self.p = 0;
        let got = self
            .fetch
            .check()
            .and_then(|_| self.fetch.fetch(self.next_off, n, &mut self.buf));
        if let Err(e) = got {
            self.err = Some(e);
            self.buf.clear();
            return false;
        }
        if self.buf.len() != n {
            self.err = Some(CmError::Corrupt("a short read"));
            self.buf.clear();
            return false;
        }
        self.next_off += n as u64;
        self.unfetched -= n as u64;
        true
    }

    /// Copies `out.len()` bytes as they are (a stored segment).
    pub fn copy_to(&mut self, out: &mut [u8]) -> Result<(), CmError> {
        let mut at = 0;
        while at < out.len() {
            if self.p == self.buf.len() && !self.refill() {
                return Err(self.take_err());
            }
            let k = (self.buf.len() - self.p).min(out.len() - at);
            if (k as u64) > self.limit {
                return Err(CmError::Corrupt("a stored segment longer than its data"));
            }
            out[at..at + k].copy_from_slice(&self.buf[self.p..self.p + k]);
            self.p += k;
            self.limit -= k as u64;
            at += k;
        }
        Ok(())
    }

    /// The error that ended reading: the fetcher's own, or "ended early".
    pub fn take_err(&mut self) -> CmError {
        self.err
            .take()
            .unwrap_or(CmError::Corrupt("the data ended early"))
    }

    pub fn check(&mut self) -> Result<(), CmError> {
        self.fetch.check()
    }
}
