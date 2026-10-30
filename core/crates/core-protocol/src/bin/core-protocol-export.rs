//! 把核协议的生成物写到给定目录：`core-protocol-export <目录>`。
//!
//! 只写文件，不删除；由 `uv run generate` 先导出到空的临时目录，再与仓库的 `protocol/` 同步或核对。

use std::path::PathBuf;
use std::process::ExitCode;
use std::{env, fs};

fn main() -> ExitCode {
    let mut args = env::args_os().skip(1);
    let (Some(out_dir), None) = (args.next(), args.next()) else {
        eprintln!("usage: core-protocol-export <output-directory>");
        return ExitCode::from(2);
    };
    let out_dir = PathBuf::from(out_dir);
    for file in core_protocol::export::files() {
        let path = out_dir.join(&file.path);
        let written = path
            .parent()
            .map_or(Ok(()), fs::create_dir_all)
            .and_then(|()| fs::write(&path, file.contents.as_bytes()));
        if let Err(error) = written {
            eprintln!(
                "core-protocol-export: cannot write {}: {error}",
                path.display()
            );
            return ExitCode::FAILURE;
        }
    }
    ExitCode::SUCCESS
}
