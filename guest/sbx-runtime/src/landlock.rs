//! Landlock confinement for processes the runtime starts (ABI ≥ 1; network rules need ABI ≥ 4, scoping ABI ≥ 6).
//! Called in the child between setsid() and the uid drop. Everything is best effort and degrades to "no confinement"
//! only when the kernel has no Landlock at all (the trimmed 6.18 guest kernel has ABI 7).
use std::ffi::CString;

const SYS_CREATE: i64 = 444; const SYS_ADD_RULE: i64 = 445; const SYS_RESTRICT: i64 = 446;
const CREATE_RULESET_VERSION: u32 = 1;
const RULE_PATH_BENEATH: u32 = 1; const RULE_NET_PORT: u32 = 2;
// fs access rights
const EXECUTE: u64 = 1; const WRITE_FILE: u64 = 2; const READ_FILE: u64 = 4; const READ_DIR: u64 = 8; const REMOVE_DIR: u64 = 16; const REMOVE_FILE: u64 = 32;
const MAKE_CHAR: u64 = 64; const MAKE_DIR: u64 = 128; const MAKE_REG: u64 = 256; const MAKE_SOCK: u64 = 512; const MAKE_FIFO: u64 = 1024; const MAKE_BLOCK: u64 = 2048; const MAKE_SYM: u64 = 4096;
const REFER: u64 = 8192; const TRUNCATE: u64 = 16384; const IOCTL_DEV: u64 = 32768;
const NET_BIND_TCP: u64 = 1; const NET_CONNECT_TCP: u64 = 2;
const SCOPE_ABSTRACT_UNIX: u64 = 1; const SCOPE_SIGNAL: u64 = 2;

#[repr(C)] struct RulesetAttr { handled_access_fs: u64, handled_access_net: u64, scoped: u64 }
#[repr(C, packed)] struct PathBeneath { allowed_access: u64, parent_fd: i32 }
#[repr(C)] struct NetPort { allowed_access: u64, port: u64 }

pub struct Policy { pub ro: Vec<String>, pub rw: Vec<String>, pub connect_ports: Vec<u16> }

pub fn abi_version() -> i64 { unsafe { libc::syscall(SYS_CREATE, std::ptr::null::<u8>(), 0usize, CREATE_RULESET_VERSION) } }

fn fs_rights(abi: i64) -> u64 {
    let mut r = EXECUTE | WRITE_FILE | READ_FILE | READ_DIR | REMOVE_DIR | REMOVE_FILE | MAKE_CHAR | MAKE_DIR | MAKE_REG | MAKE_SOCK | MAKE_FIFO | MAKE_BLOCK | MAKE_SYM;
    if abi >= 2 { r |= REFER; } if abi >= 3 { r |= TRUNCATE; } if abi >= 5 { r |= IOCTL_DEV; } r
}

/// Apply `p` to the calling process. Returns Err(reason) if confinement could not be applied.
pub fn restrict(p: &Policy) -> Result<i64, String> {
    let abi = abi_version(); if abi <= 0 { return Err("landlock unavailable".into()); }
    let all_fs = fs_rights(abi); let ro_rights = READ_FILE | READ_DIR | EXECUTE;
    let attr = RulesetAttr { handled_access_fs: all_fs, handled_access_net: if abi >= 4 { NET_BIND_TCP | NET_CONNECT_TCP } else { 0 },
                             scoped: if abi >= 6 { SCOPE_ABSTRACT_UNIX | SCOPE_SIGNAL } else { 0 } };
    let size = if abi >= 6 { 24 } else if abi >= 4 { 16 } else { 8 };
    let rs = unsafe { libc::syscall(SYS_CREATE, &attr as *const _ as *const u8, size as usize, 0u32) } as i32;
    if rs < 0 { return Err(format!("create_ruleset: {}", std::io::Error::last_os_error())); }
    let add_path = |path: &str, rights: u64| -> Result<(), String> {
        let c = CString::new(path).map_err(|_| "bad path")?;
        let fd = unsafe { libc::open(c.as_ptr(), libc::O_PATH | libc::O_CLOEXEC) };
        if fd < 0 { return Ok(()); }                                        // missing path: nothing to allow
        // a rule on a non-directory may only carry file rights, otherwise add_rule fails with EINVAL
        let mut st: libc::stat = unsafe { std::mem::zeroed() };
        let is_dir = unsafe { libc::fstat(fd, &mut st) } == 0 && (st.st_mode & libc::S_IFMT) == libc::S_IFDIR;
        let file_rights = EXECUTE | WRITE_FILE | READ_FILE | if abi >= 3 { TRUNCATE } else { 0 } | if abi >= 5 { IOCTL_DEV } else { 0 };
        let allowed = if is_dir { rights & all_fs } else { rights & file_rights };
        let rule = PathBeneath { allowed_access: allowed, parent_fd: fd };
        let r = unsafe { libc::syscall(SYS_ADD_RULE, rs, RULE_PATH_BENEATH, &rule as *const _ as *const u8, 0u32) };
        unsafe { libc::close(fd); }
        if r < 0 { return Err(format!("add_rule {path}: {}", std::io::Error::last_os_error())); } Ok(())
    };
    for d in &p.ro { add_path(d, ro_rights)?; }
    for d in &p.rw { add_path(d, all_fs)?; }
    if abi >= 4 {
        for port in &p.connect_ports {
            let rule = NetPort { allowed_access: NET_CONNECT_TCP, port: *port as u64 };
            let r = unsafe { libc::syscall(SYS_ADD_RULE, rs, RULE_NET_PORT, &rule as *const _ as *const u8, 0u32) };
            if r < 0 { return Err(format!("add_rule port {port}: {}", std::io::Error::last_os_error())); }
        }
    }
    unsafe { if libc::prctl(libc::PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0 { return Err("no_new_privs".into()); } }
    let r = unsafe { libc::syscall(SYS_RESTRICT, rs, 0u32) };
    unsafe { libc::close(rs); }
    if r < 0 { return Err(format!("restrict_self: {}", std::io::Error::last_os_error())); }
    Ok(abi)
}
