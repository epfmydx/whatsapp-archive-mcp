package main

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"golang.org/x/sys/unix"
)

var diskKeyPattern = regexp.MustCompile(`^accounts/([a-zA-Z0-9][a-zA-Z0-9_-]{0,63})/media/[a-f0-9]{64}$`)
var diskDigestPattern = regexp.MustCompile(`^[a-f0-9]{64}$`)
var errDiskMedia = errors.New("disk media unavailable or invalid")

// Walk from an anchored directory descriptor. O_NOFOLLOW applies to every
// component, including MEDIA_ROOT's ancestors, so symlink swaps cannot escape.
func diskDirectory(root string, create bool) (int, error) {
	if !filepath.IsAbs(root) || filepath.Clean(root) != root || root == "/" {
		return -1, errDiskMedia
	}
	fd, err := unix.Open("/", unix.O_RDONLY|unix.O_DIRECTORY|unix.O_CLOEXEC, 0)
	if err != nil {
		return -1, err
	}
	for _, part := range strings.Split(strings.TrimPrefix(root, "/"), "/") {
		next, e := unix.Openat(fd, part, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_CLOEXEC|unix.O_NOFOLLOW, 0)
		if e == unix.ENOENT && create {
			e = unix.Mkdirat(fd, part, 0700)
			if e == nil {
				e = unix.Fsync(fd)
			}
			if e == nil || e == unix.EEXIST {
				next, e = unix.Openat(fd, part, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_CLOEXEC|unix.O_NOFOLLOW, 0)
			}
		}
		unix.Close(fd)
		if e != nil {
			return -1, errDiskMedia
		}
		fd = next
	}
	return fd, nil
}

func diskMediaDirectory(c config) (int, error) {
	fd, err := diskDirectory(c.MediaRoot, true)
	if err != nil {
		return -1, err
	}
	// This is a dedicated private archive root, shared by the same service UID.
	if err = unix.Fchmod(fd, 0700); err != nil {
		unix.Close(fd)
		return -1, err
	}
	for _, part := range []string{"accounts", c.Account, "media"} {
		if !accountPattern.MatchString(c.Account) {
			unix.Close(fd)
			return -1, errDiskMedia
		}
		if err = unix.Mkdirat(fd, part, 0700); err != nil && err != unix.EEXIST {
			unix.Close(fd)
			return -1, errDiskMedia
		}
		if err = unix.Fsync(fd); err != nil {
			unix.Close(fd)
			return -1, err
		}
		next, e := unix.Openat(fd, part, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_CLOEXEC|unix.O_NOFOLLOW, 0)
		unix.Close(fd)
		if e != nil {
			return -1, errDiskMedia
		}
		fd = next
		if err = unix.Fchmod(fd, 0700); err != nil {
			unix.Close(fd)
			return -1, err
		}
	}
	return fd, nil
}

func initializeDisk(c config) error {
	fd, err := diskMediaDirectory(c)
	if err == nil {
		unix.Close(fd)
	}
	return err
}

type diskContextReader struct {
	ctx    context.Context
	source io.Reader
}

func (r diskContextReader) Read(p []byte) (int, error) {
	if err := r.ctx.Err(); err != nil {
		return 0, err
	}
	return r.source.Read(p)
}

func verifyDiskFile(fd int, name string, size int64, digest string) error {
	raw, err := unix.Openat(fd, name, unix.O_RDONLY|unix.O_CLOEXEC|unix.O_NOFOLLOW|unix.O_NONBLOCK, 0)
	if err != nil {
		return errDiskMedia
	}
	f := os.NewFile(uintptr(raw), "archive-media")
	defer f.Close()
	st, err := f.Stat()
	if err != nil || !st.Mode().IsRegular() || st.Mode().Perm() != 0600 || st.Size() != size {
		return errDiskMedia
	}
	h := sha256.New()
	n, err := io.Copy(h, io.LimitReader(f, size+1))
	if err != nil || n != size || hex.EncodeToString(h.Sum(nil)) != digest {
		return errDiskMedia
	}
	return nil
}

// Publish a durable verified private inode without replacing an existing object.
// Cache files remain available to the existing recovery/retry machinery on error.
func persistDisk(ctx context.Context, c config, key string, source io.Reader, size int64, digest string) error {
	parts := diskKeyPattern.FindStringSubmatch(key)
	if parts == nil || parts[1] != c.Account || size < 0 || size > c.MaxMedia || !diskDigestPattern.MatchString(digest) {
		return errDiskMedia
	}
	fd, err := diskMediaDirectory(c)
	if err != nil {
		return err
	}
	defer unix.Close(fd)
	var nonce [16]byte
	if _, err = rand.Read(nonce[:]); err != nil {
		return err
	}
	temp := ".tmp-" + hex.EncodeToString(nonce[:])
	raw, err := unix.Openat(fd, temp, unix.O_WRONLY|unix.O_CREAT|unix.O_EXCL|unix.O_CLOEXEC|unix.O_NOFOLLOW, 0600)
	if err != nil {
		return errDiskMedia
	}
	f := os.NewFile(uintptr(raw), "archive-temporary")
	defer func() { f.Close(); unix.Unlinkat(fd, temp, 0) }()
	h := sha256.New()
	n, err := io.Copy(io.MultiWriter(f, h), io.LimitReader(diskContextReader{ctx, source}, size+1))
	if err != nil {
		return err
	}
	if n != size || hex.EncodeToString(h.Sum(nil)) != digest {
		return errDiskMedia
	}
	if err = f.Sync(); err != nil {
		return err
	}
	if err = f.Close(); err != nil {
		return err
	}
	if err = ctx.Err(); err != nil {
		return err
	}
	name := filepath.Base(key)
	err = unix.Linkat(fd, temp, fd, name, 0)
	if err == unix.EEXIST {
		err = verifyDiskFile(fd, name, size, digest)
	}
	if err != nil {
		return errDiskMedia
	}
	if err = unix.Unlinkat(fd, temp, 0); err != nil {
		return err
	}
	return unix.Fsync(fd)
}
