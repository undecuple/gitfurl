//! The Python module `gitfurl._codecs`: the cm-stage and rolz-stage decoders for gitfurl's
//! farc_codecs.
//!
//! rolz: `rolz_block(read, size, budget, chunk, limit, stop)` gives an iterator of the block's
//! output pieces; it decodes with the interpreter released and calls `read(offset, n)` (one
//! packet at a time) and `stop()` with it held. Refusals raise `RolzError` with the same text
//! as gitfurl.farc_rolz; an exception from `read` or `stop` is raised as it is.
//!
//! cm:
//! Decoding runs with the interpreter released, on up to `threads` threads (one job each).
//! The compressed bytes come either from a `bytes` object, or through a Python callable
//! `read(offset, n) -> bytes` that each worker calls (with the interpreter held) a chunk at a
//! time, so a job's compressed data is never held whole. `stop()` is called between chunks
//! and every few segments; an exception from it, or from `read`, ends the decode and is
//! raised as it is. Damaged data and variants this decoder does not cover raise `CmError`.

use std::sync::Mutex;

use pyo3::create_exception;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;

use crate::cm::input::Fetch;
use crate::cm::{self, CmError as Error, JobSpan};
use crate::rolz::{self, RolzError as RzError};

create_exception!(
    _codecs,
    CmError,
    PyValueError,
    "Damaged cm-stage data, or a variant gitfurl does not decode."
);

create_exception!(
    _codecs,
    RolzError,
    PyValueError,
    "Damaged rolz-stage data, or more than gitfurl allows (the text says which)."
);

/// Jobs one cm_decode_jobs call may decode (each holds up to 512 MiB of output).
const MAX_JOBS_AT_ONCE: usize = 64;

fn to_py(e: Error) -> PyErr {
    CmError::new_err(e.to_string())
}

/// Compressed bytes read through Python callables, with the first Python error kept.
struct PyFetch<'a> {
    read: &'a Py<PyAny>,
    stop: Option<&'a Py<PyAny>>,
    err: &'a Mutex<Option<PyErr>>,
}

impl PyFetch<'_> {
    fn keep(&self, e: PyErr) -> Error {
        if let Ok(mut slot) = self.err.lock() {
            slot.get_or_insert(e);
        }
        Error::External
    }
}

impl Fetch for PyFetch<'_> {
    fn fetch(&mut self, off: u64, n: usize, buf: &mut Vec<u8>) -> Result<(), Error> {
        Python::attach(|py| {
            let got = self.read.call1(py, (off, n)).map_err(|e| self.keep(e))?;
            let got = got.bind(py);
            let b = got
                .cast::<PyBytes>()
                .map_err(|e| self.keep(PyErr::from(e)))?;
            let data = b.as_bytes();
            if data.len() != n {
                return Err(Error::Corrupt("a short read"));
            }
            buf.extend_from_slice(data);
            Ok(())
        })
    }

    fn check(&mut self) -> Result<(), Error> {
        match self.stop {
            None => Ok(()),
            Some(stop) => Python::attach(|py| stop.call0(py).map(|_| ()).map_err(|e| self.keep(e))),
        }
    }
}

/// One job's decoded bytes, handed out a piece at a time.
#[pyclass(module = "gitfurl._codecs")]
struct CmOutput {
    data: Vec<u8>,
    len: usize,
    at: usize,
}

#[pymethods]
impl CmOutput {
    fn __len__(&self) -> usize {
        self.len
    }

    /// Bytes not taken yet.
    #[getter]
    fn left(&self) -> usize {
        self.len - self.at
    }

    /// The next 1 to n bytes, or b"" once all were taken; the memory is freed then.
    fn take<'py>(&mut self, py: Python<'py>, n: usize) -> Bound<'py, PyBytes> {
        let k = n.max(1).min(self.len - self.at);
        let Some(piece) = self.data.get(self.at..self.at + k) else {
            return PyBytes::new(py, b"");
        };
        let out = PyBytes::new(py, piece);
        self.at += k;
        if self.at == self.len {
            self.data = Vec::new();
        }
        out
    }
}

/// Checks the stream header; returns (job size, window size) in bytes.
#[pyfunction]
fn cm_header(head: &[u8]) -> PyResult<(usize, u64)> {
    let p = cm::parse_header(head).map_err(to_py)?;
    Ok((p.job_size, p.window))
}

/// Decodes a whole stream held in memory.
#[pyfunction]
#[pyo3(signature = (data, threads = 1))]
fn cm_decode<'py>(
    py: Python<'py>,
    data: Bound<'py, PyBytes>,
    threads: usize,
) -> PyResult<Bound<'py, PyBytes>> {
    let bytes = data.as_bytes();
    let out = py.detach(|| cm::decode(bytes, threads)).map_err(to_py)?;
    Ok(PyBytes::new(py, &out))
}

/// Decodes the jobs `jobs` (each (a_offset, a_length, b_offset, b_length) in the compressed
/// data) on up to `threads` threads; returns one CmOutput per job, in order.
#[pyfunction]
#[pyo3(signature = (head, jobs, read, threads = 1, stop = None))]
fn cm_decode_jobs(
    py: Python<'_>,
    head: &[u8],
    jobs: Vec<(u64, u64, u64, u64)>,
    read: Py<PyAny>,
    threads: usize,
    stop: Option<Py<PyAny>>,
) -> PyResult<Vec<CmOutput>> {
    let p = cm::parse_header(head).map_err(to_py)?;
    if jobs.len() > MAX_JOBS_AT_ONCE {
        return Err(PyValueError::new_err("too many cm-stage jobs in one call"));
    }
    let spans: Vec<JobSpan> = jobs
        .iter()
        .map(|&(a_off, a_len, b_off, b_len)| JobSpan {
            a_off,
            a_len,
            b_off,
            b_len,
        })
        .collect();
    let errs: Vec<Mutex<Option<PyErr>>> = spans.iter().map(|_| Mutex::new(None)).collect();
    let mut bufs: Vec<Vec<u8>> = Vec::new();
    for _ in &spans {
        let mut b = Vec::new();
        b.try_reserve_exact(p.job_size).map_err(|_| {
            pyo3::exceptions::PyMemoryError::new_err("not enough memory for a cm-stage job")
        })?;
        b.resize(p.job_size, 0);
        bufs.push(b);
    }
    let results = py.detach(|| {
        let mut views: Vec<&mut [u8]> = bufs.iter_mut().map(|b| b.as_mut_slice()).collect();
        cm::decode_jobs(&p, &spans, &mut views, threads, |i| PyFetch {
            read: &read,
            stop: stop.as_ref(),
            err: &errs[i],
        })
    });
    let mut outs = Vec::new();
    for ((r, data), err) in results.into_iter().zip(bufs).zip(errs) {
        match r {
            Ok(len) => outs.push(CmOutput { data, len, at: 0 }),
            Err(Error::External) => {
                let kept = err.into_inner().ok().flatten();
                return Err(kept.unwrap_or_else(|| to_py(Error::External)));
            }
            Err(e) => return Err(to_py(e)),
        }
    }
    Ok(outs)
}

/// The number of jobs of a whole stream held in memory (its header and job list checked).
#[pyfunction]
fn cm_job_count(data: &[u8]) -> PyResult<usize> {
    let (_, spans) = cm::walk(data).map_err(to_py)?;
    Ok(spans.len())
}

// --- rolz ---------------------------------------------------------------------------------------

/// The block's bytes through Python callables, with the first Python error kept.
struct PySource<'a> {
    read: &'a Py<PyAny>,
    stop: Option<&'a Py<PyAny>>,
    err: &'a mut Option<PyErr>,
}

impl PySource<'_> {
    fn keep(&mut self, e: PyErr) -> RzError {
        self.err.get_or_insert(e);
        RzError::External
    }
}

impl rolz::Source for PySource<'_> {
    fn read(&mut self, off: u64, n: usize) -> Result<Vec<u8>, RzError> {
        let got = Python::attach(|py| -> PyResult<Vec<u8>> {
            let got = self.read.call1(py, (off, n))?;
            let b = got.bind(py).cast::<PyBytes>().map_err(PyErr::from)?;
            Ok(b.as_bytes().to_vec())
        });
        match got {
            Ok(v) if v.len() == n => Ok(v),
            Ok(_) => Err(RzError::Data("damaged data (a short read)".into())),
            Err(e) => Err(self.keep(e)),
        }
    }

    fn stop(&mut self) -> Result<(), RzError> {
        let Some(stop) = self.stop else {
            return Ok(());
        };
        match Python::attach(|py| stop.call0(py).map(|_| ())) {
            Ok(()) => Ok(()),
            Err(e) => Err(self.keep(e)),
        }
    }
}

fn rz_err(e: RzError, kept: Option<PyErr>) -> PyErr {
    match e {
        RzError::Data(m) => RolzError::new_err(m.into_owned()),
        RzError::Memory => {
            pyo3::exceptions::PyMemoryError::new_err("not enough memory for the rolz stage")
        }
        RzError::External => kept.unwrap_or_else(|| RolzError::new_err(e.to_string())),
    }
}

/// The output of one rolz block, a piece at a time (an iterator of bytes).
#[pyclass(module = "gitfurl._codecs")]
struct RolzBlock {
    inner: rolz::Block,
    read: Py<PyAny>,
    stop: Option<Py<PyAny>>,
}

#[pymethods]
impl RolzBlock {
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    fn __next__<'py>(&mut self, py: Python<'py>) -> PyResult<Option<Bound<'py, PyBytes>>> {
        let mut kept = None;
        let inner = &mut self.inner;
        let (read, stop) = (&self.read, self.stop.as_ref());
        let r = py.detach(|| {
            let mut src = PySource {
                read,
                stop,
                err: &mut kept,
            };
            inner.next_piece(&mut src)
        });
        match r {
            Ok(Some(piece)) => Ok(Some(PyBytes::new(py, &piece))),
            Ok(None) => Ok(None),
            Err(e) => Err(rz_err(e, kept)),
        }
    }

    /// (window, size, crc, main LZ bytes, table LZ bytes) of the last finished record.
    #[getter]
    fn last_record(&self) -> Option<(u64, u64, u32, u64, u64)> {
        self.inner
            .last
            .map(|r| (r.window, r.size, r.crc, r.main, r.tables))
    }
}

/// The rolz block of `size` bytes read through `read(offset, n)`; `budget` is the largest
/// window accepted, `limit` the most output accepted.
#[pyfunction]
#[pyo3(signature = (read, size, budget, chunk = 1 << 20, limit = None, stop = None))]
fn rolz_block(
    read: Py<PyAny>,
    size: u64,
    budget: u64,
    chunk: usize,
    limit: Option<u64>,
    stop: Option<Py<PyAny>>,
) -> RolzBlock {
    RolzBlock {
        inner: rolz::Block::new(size, budget, chunk, limit),
        read,
        stop,
    }
}

/// The whole output of a rolz block held in memory.
#[pyfunction]
#[pyo3(signature = (data, budget, chunk = 1 << 20, limit = None))]
fn rolz_decode<'py>(
    py: Python<'py>,
    data: &[u8],
    budget: u64,
    chunk: usize,
    limit: Option<u64>,
) -> PyResult<Bound<'py, PyBytes>> {
    let out = py
        .detach(|| rolz::decode(data, budget, chunk, limit))
        .map_err(|e| rz_err(e, None))?;
    Ok(PyBytes::new(py, &out))
}

/// Bytes one rolz record may hold at most, for a window of `window` bytes.
#[pyfunction]
fn rolz_memory_for(window: u64) -> u64 {
    rolz::memory_for(window)
}

#[pymodule]
fn _codecs(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("CmError", m.py().get_type::<CmError>())?;
    m.add("RolzError", m.py().get_type::<RolzError>())?;
    m.add("ROLZ_WINDOW_CAP", rolz::WINDOW_CAP)?;
    m.add_class::<RolzBlock>()?;
    m.add_function(wrap_pyfunction!(rolz_block, m)?)?;
    m.add_function(wrap_pyfunction!(rolz_decode, m)?)?;
    m.add_function(wrap_pyfunction!(rolz_memory_for, m)?)?;
    m.add("HEADER_LEN", cm::HEADER_LEN)?;
    m.add("MAX_JOB", cm::MAX_JOB)?;
    m.add_class::<CmOutput>()?;
    m.add_function(wrap_pyfunction!(cm_header, m)?)?;
    m.add_function(wrap_pyfunction!(cm_decode, m)?)?;
    m.add_function(wrap_pyfunction!(cm_decode_jobs, m)?)?;
    m.add_function(wrap_pyfunction!(cm_job_count, m)?)?;
    Ok(())
}
