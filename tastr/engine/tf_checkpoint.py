"""Read a TensorFlow 2 checkpoint (`<prefix>.index` + `<prefix>.data-00000-of-00001`) without TensorFlow.

The index is a LevelDB-style table (uncompressed blocks of prefix-compressed keys) whose values are
`BundleEntryProto` messages: dtype, shape, and where the tensor's bytes sit in the data file. Only what
the ADTOF checkpoint uses is supported: one data shard, float32/int64 tensors, no partitioned variables.
"""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

_MAGIC = 0xDB4775248B80FB57
_DTYPES = {1: np.float32, 2: np.float64, 3: np.int32, 9: np.int64}  # tensorflow DataType enum


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


def _block(buf: bytes, offset: int, size: int) -> list[tuple[bytes, bytes]]:
    """Key/value entries of one table block (the 5-byte trailer after it is compression type + crc)."""
    if buf[offset + size] != 0:
        raise ValueError("compressed checkpoint index blocks are not supported")
    data = buf[offset:offset + size]
    n_restarts = struct.unpack_from("<I", data, len(data) - 4)[0]
    end = len(data) - 4 - 4 * n_restarts
    out, key, i = [], b"", 0
    while i < end:
        shared, i = _varint(data, i)
        fresh, i = _varint(data, i)
        vlen, i = _varint(data, i)
        key = key[:shared] + data[i:i + fresh]
        i += fresh
        out.append((key, data[i:i + vlen]))
        i += vlen
    return out


def _fields(msg: bytes) -> dict[int, list]:
    """Protobuf wire format: {field number: [values]} (varints as ints, length-delimited as bytes)."""
    out: dict[int, list] = {}
    i = 0
    while i < len(msg):
        tag, i = _varint(msg, i)
        num, wire = tag >> 3, tag & 7
        if wire == 0:
            v, i = _varint(msg, i)
        elif wire == 2:
            n, i = _varint(msg, i)
            v, i = msg[i:i + n], i + n
        elif wire == 5:
            v, i = msg[i:i + 4], i + 4
        elif wire == 1:
            v, i = msg[i:i + 8], i + 8
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        out.setdefault(num, []).append(v)
    return out


def read(prefix: str | Path) -> dict[str, np.ndarray]:
    """{variable name: array} for every tensor in the checkpoint at `prefix`."""
    prefix = str(prefix)
    index = Path(prefix + ".index").read_bytes()
    data = Path(prefix + ".data-00000-of-00001").read_bytes()
    if struct.unpack_from("<Q", index, len(index) - 8)[0] != _MAGIC:
        raise ValueError(f"{prefix}.index is not a TensorFlow checkpoint index")
    footer = index[len(index) - 48:]
    _, j = _varint(footer, 0)  # metaindex handle (unused)
    _, j = _varint(footer, j)
    idx_off, j = _varint(footer, j)
    idx_size, j = _varint(footer, j)
    tensors = {}
    for _, handle in _block(index, idx_off, idx_size):
        off, k = _varint(handle, 0)
        size, _ = _varint(handle, k)
        for key, value in _block(index, off, size):
            if not key:
                continue  # the bundle header
            f = _fields(value)
            dtype = _DTYPES.get(f.get(1, [0])[0])
            if dtype is None:
                continue  # strings (object graph, optimizer state names) are not needed
            shape = [(_fields(d).get(1, [0])[0]) for d in _fields(f[2][0]).get(2, [])] if 2 in f else []
            start, nbytes = f.get(4, [0])[0], f.get(5, [0])[0]
            arr = np.frombuffer(data, dtype=dtype, count=nbytes // np.dtype(dtype).itemsize, offset=start)
            tensors[key.decode()] = arr.reshape(shape).copy()
    return tensors
