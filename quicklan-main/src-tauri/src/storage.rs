use serde::Serialize;
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::Read,
    path::{Path, PathBuf},
};

pub fn app_data_dir() -> PathBuf {
    dirs::data_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join("QuickLANData")
}

pub fn config_dir() -> PathBuf {
    dirs::config_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join("QuickLAN")
}

pub fn database_path() -> PathBuf {
    app_data_dir().join("quicklan.sqlite3")
}

pub fn shared_store_dir() -> PathBuf {
    app_data_dir().join("shared_store")
}

pub fn shared_content_path(file_hash: &str) -> PathBuf {
    shared_store_dir().join(file_hash).join("content.bin")
}

pub fn shared_metadata_path(file_hash: &str) -> PathBuf {
    shared_store_dir().join(file_hash).join("metadata.json")
}

pub async fn ensure_download_dir(dir: PathBuf) -> Result<PathBuf, String> {
    tokio::fs::create_dir_all(&dir)
        .await
        .map_err(|err| format!("创建保存目录失败: {err}"))?;
    Ok(dir)
}

pub fn ensure_app_dirs() -> Result<(), String> {
    fs::create_dir_all(shared_store_dir()).map_err(|err| format!("创建 QuickLANData 失败: {err}"))
}

pub fn safe_file_name(name: &str) -> String {
    let cleaned: String = name
        .chars()
        .map(|c| match c {
            '<' | '>' | ':' | '"' | '/' | '\\' | '|' | '?' | '*' => '_',
            _ if c.is_control() => '_',
            _ => c,
        })
        .collect();

    let trimmed = cleaned.trim().trim_matches('.');
    if trimmed.is_empty() {
        "quicklan-file".to_string()
    } else {
        trimmed.to_string()
    }
}

pub async fn unique_destination(dir: &Path, file_name: &str) -> Result<PathBuf, String> {
    let safe = safe_file_name(file_name);
    let candidate = dir.join(&safe);
    if tokio::fs::metadata(&candidate).await.is_err() {
        return Ok(candidate);
    }

    let path = Path::new(&safe);
    let stem = path
        .file_stem()
        .and_then(|value| value.to_str())
        .unwrap_or("quicklan-file");
    let ext = path.extension().and_then(|value| value.to_str());

    for idx in 1..10000 {
        let next_name = match ext {
            Some(ext) => format!("{stem} ({idx}).{ext}"),
            None => format!("{stem} ({idx})"),
        };
        let next = dir.join(next_name);
        if tokio::fs::metadata(&next).await.is_err() {
            return Ok(next);
        }
    }

    Err("无法生成不冲突的保存文件名".to_string())
}

pub fn path_file_name(path: &Path) -> Result<String, String> {
    path.file_name()
        .and_then(|value| value.to_str())
        .map(|value| value.to_string())
        .ok_or_else(|| format!("无法读取文件名: {}", path.display()))
}

pub fn collect_files(paths: Vec<String>) -> Result<Vec<PathBuf>, String> {
    let mut files = Vec::new();
    for path in paths {
        let path = PathBuf::from(path);
        if path.is_file() {
            files.push(path);
        } else if path.is_dir() {
            collect_dir(&path, &mut files)?;
        }
    }
    files.sort();
    files.dedup();
    Ok(files)
}

fn collect_dir(dir: &Path, files: &mut Vec<PathBuf>) -> Result<(), String> {
    for entry in fs::read_dir(dir).map_err(|err| format!("读取文件夹失败 {}: {err}", dir.display()))? {
        let entry = entry.map_err(|err| format!("读取文件夹项目失败: {err}"))?;
        let path = entry.path();
        if path.is_file() {
            files.push(path);
        } else if path.is_dir() {
            collect_dir(&path, files)?;
        }
    }
    Ok(())
}

pub fn copy_to_shared_store<T: Serialize>(
    source: &Path,
    file_hash: &str,
    metadata: &T,
) -> Result<PathBuf, String> {
    copy_to_shared_store_in(
        &shared_store_dir(),
        source,
        file_hash,
        metadata,
        Some(shared_metadata_path(file_hash)),
    )
}

fn copy_to_shared_store_in<T: Serialize>(
    store_dir: &Path,
    source: &Path,
    file_hash: &str,
    metadata: &T,
    metadata_path: Option<PathBuf>,
) -> Result<PathBuf, String> {
    let dir = store_dir.join(file_hash);
    fs::create_dir_all(&dir).map_err(|err| format!("创建共享副本目录失败: {err}"))?;

    let content_path = dir.join("content.bin");
    if content_path.exists() {
        validate_content_hash(&content_path, file_hash)?;
    } else {
        fs::copy(source, &content_path)
            .map_err(|err| format!("创建共享副本失败 {}: {err}", source.display()))?;
        validate_content_hash(&content_path, file_hash)?;
    }

    let metadata_path = metadata_path.unwrap_or_else(|| dir.join("metadata.json"));
    let content = serde_json::to_string_pretty(metadata)
        .map_err(|err| format!("序列化共享元数据失败: {err}"))?;
    fs::write(metadata_path, content).map_err(|err| format!("写入共享元数据失败: {err}"))?;
    Ok(content_path)
}

pub fn validate_shared_content(file_hash: &str) -> Result<PathBuf, String> {
    let content_path = shared_content_path(file_hash);
    validate_content_hash(&content_path, file_hash)?;
    Ok(content_path)
}

fn validate_content_hash(path: &Path, expected_hash: &str) -> Result<(), String> {
    if !path.is_file() {
        return Err(format!("共享副本文件不存在: {}", path.display()));
    }
    let actual = sha256_file(path)?;
    if actual != expected_hash {
        return Err(format!(
            "共享副本校验失败: expected {expected_hash}, got {actual}"
        ));
    }
    Ok(())
}

fn sha256_file(path: &Path) -> Result<String, String> {
    let mut file = fs::File::open(path)
        .map_err(|err| format!("打开共享副本失败 {}: {err}", path.display()))?;
    let mut hasher = Sha256::new();
    let mut buf = vec![0_u8; crate::protocol::CHUNK_SIZE];
    loop {
        let read = file
            .read(&mut buf)
            .map_err(|err| format!("读取共享副本失败: {err}"))?;
        if read == 0 {
            break;
        }
        hasher.update(&buf[..read]);
    }
    Ok(format!("{:x}", hasher.finalize()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde::Serialize;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[derive(Serialize)]
    struct TestMetadata {
        name: String,
    }

    #[test]
    fn shared_store_snapshot_survives_original_modification() {
        let dir = temp_dir("snapshot-original");
        let source = dir.join("source.txt");
        fs::write(&source, b"A").unwrap();
        let hash = sha256_file(&source).unwrap();

        let content_path = copy_to_shared_store_in(
            &dir.join("store"),
            &source,
            &hash,
            &TestMetadata {
                name: "source.txt".to_string(),
            },
            None,
        )
        .unwrap();
        fs::write(&source, b"B").unwrap();

        assert_eq!(fs::read(&content_path).unwrap(), b"A");
        validate_content_hash(&content_path, &hash).unwrap();
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn shared_store_snapshot_survives_visible_download_modification() {
        let dir = temp_dir("snapshot-download");
        let source = dir.join("downloaded.txt");
        fs::write(&source, b"stable").unwrap();
        let hash = sha256_file(&source).unwrap();
        let content_path = copy_to_shared_store_in(
            &dir.join("store"),
            &source,
            &hash,
            &TestMetadata {
                name: "downloaded.txt".to_string(),
            },
            None,
        )
        .unwrap();

        let visible = dir.join("visible.txt");
        fs::copy(&content_path, &visible).unwrap();
        fs::write(&visible, b"user edit").unwrap();

        assert_eq!(fs::read(&content_path).unwrap(), b"stable");
        validate_content_hash(&content_path, &hash).unwrap();
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn same_hash_reuses_valid_shared_store_copy() {
        let dir = temp_dir("snapshot-reuse");
        let source_a = dir.join("a.txt");
        let source_b = dir.join("b.txt");
        fs::write(&source_a, b"same").unwrap();
        fs::write(&source_b, b"same").unwrap();
        let hash = sha256_file(&source_a).unwrap();
        let store = dir.join("store");

        let first = copy_to_shared_store_in(
            &store,
            &source_a,
            &hash,
            &TestMetadata {
                name: "a.txt".to_string(),
            },
            None,
        )
        .unwrap();
        let second = copy_to_shared_store_in(
            &store,
            &source_b,
            &hash,
            &TestMetadata {
                name: "b.txt".to_string(),
            },
            None,
        )
        .unwrap();

        assert_eq!(first, second);
        assert_eq!(fs::read(&second).unwrap(), b"same");
        validate_content_hash(&second, &hash).unwrap();
        let _ = fs::remove_dir_all(dir);
    }

    fn temp_dir(label: &str) -> PathBuf {
        let stamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let dir = std::env::temp_dir().join(format!("quicklan-{label}-{stamp}"));
        fs::create_dir_all(&dir).unwrap();
        dir
    }
}
