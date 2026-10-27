package main

import (
	"errors"
	"io"
	"os"

	"go.mau.fi/whatsmeow"
)

var errMediaBoundExceeded = errors.New("media bound exceeded")

// Use a named field: embedding *os.File promotes ReadFrom, letting io.Copy
// bypass Write's bound. The upstream File interface requires only these methods.
// Not being *os.File also prevents untrusted Content-Length preallocation.
type boundedFile struct {
	file  *os.File
	limit int64
}

func (f boundedFile) Read(p []byte) (int, error)              { return f.file.Read(p) }
func (f boundedFile) ReadAt(p []byte, off int64) (int, error) { return f.file.ReadAt(p, off) }
func (f boundedFile) Stat() (os.FileInfo, error)              { return f.file.Stat() }

func (f boundedFile) Write(p []byte) (int, error) {
	pos, err := f.file.Seek(0, io.SeekCurrent)
	if err != nil {
		return 0, err
	}
	if pos > f.limit-int64(len(p)) {
		return 0, errMediaBoundExceeded
	}
	return f.file.Write(p)
}

func (f boundedFile) WriteAt(p []byte, off int64) (int, error) {
	if off < 0 || off > f.limit-int64(len(p)) {
		return 0, errMediaBoundExceeded
	}
	return f.file.WriteAt(p, off)
}

func (f boundedFile) Truncate(n int64) error {
	if n < 0 || n > f.limit {
		return errMediaBoundExceeded
	}
	return f.file.Truncate(n)
}

func (f boundedFile) Seek(off int64, whence int) (int64, error) {
	var base int64
	switch whence {
	case io.SeekStart:
	case io.SeekCurrent:
		pos, err := f.file.Seek(0, io.SeekCurrent)
		if err != nil {
			return 0, err
		}
		base = pos
	case io.SeekEnd:
		stat, err := f.file.Stat()
		if err != nil {
			return 0, err
		}
		base = stat.Size()
	default:
		return 0, errors.New("invalid seek")
	}
	if off < -base || off > f.limit-base {
		return 0, errMediaBoundExceeded
	}
	return f.file.Seek(off, whence)
}

var _ whatsmeow.File = boundedFile{}
