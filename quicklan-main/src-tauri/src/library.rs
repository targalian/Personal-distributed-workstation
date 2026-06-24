use crate::{
    protocol::{
        DiscoveryPacket, LibrarySettings, Manifest, ManifestShare, ShareItem, ShareVersion,
        TCP_PORT,
    },
    storage,
};
use rusqlite::{params, Connection, OptionalExtension};
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::{
    collections::HashSet,
    fs::File,
    io::Read,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::{SystemTime, UNIX_EPOCH},
};
use uuid::Uuid;

#[derive(Clone)]
pub struct LibraryService {
    inner: Arc<Mutex<LibraryInner>>,
}

struct LibraryInner {
    conn: Connection,
    device_id: String,
    device_name: String,
}

#[derive(Debug, Clone)]
pub struct LibrarySummary {
    pub library_version: i64,
    pub share_count: i64,
    pub manifest_hash: String,
}

#[derive(Debug, Clone)]
pub struct DownloadSource {
    pub share_id: String,
    pub version: i64,
    pub file_hash: String,
    pub name: String,
    pub size: u64,
    pub device_id: String,
    pub device_name: String,
    pub ip: String,
    pub tcp_port: u16,
    pub api_port: u16,
    pub is_local: bool,
}

#[derive(Debug, Clone)]
pub struct SharedContent {
    pub share_id: String,
    pub version: i64,
    pub file_hash: String,
    pub name: String,
    pub size: u64,
    pub path: PathBuf,
}

#[derive(Debug, Serialize)]
struct StoreMetadata {
    share_id: String,
    version: i64,
    file_hash: String,
    name: String,
    size: u64,
    created_at: i64,
}

impl LibraryService {
    pub fn load(device_id: String, device_name: String) -> Result<Self, String> {
        storage::ensure_app_dirs()?;
        let db_path = storage::database_path();
        if let Some(parent) = db_path.parent() {
            std::fs::create_dir_all(parent)
                .map_err(|err| format!("创建数据库目录失败: {err}"))?;
        }
        let conn = Connection::open(db_path).map_err(|err| format!("打开 SQLite 失败: {err}"))?;
        let service = Self {
            inner: Arc::new(Mutex::new(LibraryInner {
                conn,
                device_id,
                device_name,
            })),
        };
        service.init()?;
        Ok(service)
    }

    pub fn set_device_name(&self, name: String) {
        if let Ok(mut inner) = self.inner.lock() {
            inner.device_name = name;
        }
    }

    pub fn device_id(&self) -> String {
        self.inner
            .lock()
            .map(|inner| inner.device_id.clone())
            .unwrap_or_default()
    }

    pub fn device_name(&self) -> String {
        self.inner
            .lock()
            .map(|inner| inner.device_name.clone())
            .unwrap_or_else(|_| "QuickLAN".to_string())
    }

    pub fn device_note(&self, device_id: &str) -> Option<String> {
        let Ok(inner) = self.inner.lock() else {
            return None;
        };
        inner
            .conn
            .query_row(
                "SELECT note FROM device_notes WHERE device_id = ?1",
                params![device_id],
                |row| row.get::<_, String>(0),
            )
            .ok()
            .filter(|note| !note.trim().is_empty())
    }

    pub fn update_device_note(&self, device_id: &str, note: String) -> Result<Option<String>, String> {
        let note = clean_device_note(&note);
        let inner = self.lock()?;
        if note.is_empty() {
            inner
                .conn
                .execute("DELETE FROM device_notes WHERE device_id = ?1", params![device_id])
                .map_err(|err| format!("删除设备备注失败: {err}"))?;
            Ok(None)
        } else {
            inner
                .conn
                .execute(
                    "INSERT OR REPLACE INTO device_notes (device_id, note, updated_at)
                     VALUES (?1, ?2, ?3)",
                    params![device_id, note, now_secs()],
                )
                .map_err(|err| format!("保存设备备注失败: {err}"))?;
            Ok(Some(note))
        }
    }

    fn init(&self) -> Result<(), String> {
        let inner = self.lock()?;
        inner
            .conn
            .execute_batch(
                "
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS library_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    version INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO library_state (id, version) VALUES (1, 1);

                CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    acceleration_enabled INTEGER NOT NULL,
                    max_upload_speed TEXT NOT NULL,
                    max_upload_tasks INTEGER NOT NULL,
                    cache_limit_gb INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO settings
                    (id, acceleration_enabled, max_upload_speed, max_upload_tasks, cache_limit_gb)
                    VALUES (1, 1, 'unlimited', 3, 50);

                CREATE TABLE IF NOT EXISTS shares (
                    share_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    category TEXT NOT NULL,
                    permission TEXT NOT NULL,
                    password TEXT,
                    owner_device_id TEXT NOT NULL,
                    owner_name TEXT NOT NULL,
                    active INTEGER NOT NULL,
                    is_local INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    download_count INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS versions (
                    share_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    file_hash TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY (share_id, version)
                );

                CREATE TABLE IF NOT EXISTS replicas (
                    share_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    file_hash TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    device_name TEXT NOT NULL,
                    ip TEXT NOT NULL,
                    tcp_port INTEGER NOT NULL,
                    api_port INTEGER NOT NULL,
                    online INTEGER NOT NULL,
                    upload_tasks INTEGER NOT NULL,
                    latency_ms INTEGER,
                    is_local INTEGER NOT NULL,
                    last_seen INTEGER NOT NULL,
                    PRIMARY KEY (share_id, version, file_hash, device_id)
                );

                CREATE TABLE IF NOT EXISTS devices (
                    device_id TEXT PRIMARY KEY,
                    device_name TEXT NOT NULL,
                    ip TEXT NOT NULL,
                    tcp_port INTEGER NOT NULL,
                    api_port INTEGER NOT NULL,
                    library_version INTEGER NOT NULL,
                    share_count INTEGER NOT NULL,
                    manifest_hash TEXT NOT NULL,
                    upload_tasks INTEGER NOT NULL,
                    latency_ms INTEGER,
                    online INTEGER NOT NULL,
                    last_seen INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS device_notes (
                    device_id TEXT PRIMARY KEY,
                    note TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                ",
            )
            .map_err(|err| format!("初始化 SQLite 失败: {err}"))?;
        Ok(())
    }

    pub fn summary(&self) -> LibrarySummary {
        let manifest_hash = self
            .local_manifest()
            .map(|manifest| manifest.manifest_hash)
            .unwrap_or_else(|_| "empty".to_string());
        let Ok(inner) = self.inner.lock() else {
            return LibrarySummary {
                library_version: 1,
                share_count: 0,
                manifest_hash,
            };
        };
        let library_version = inner
            .conn
            .query_row("SELECT version FROM library_state WHERE id = 1", [], |row| row.get(0))
            .unwrap_or(1);
        let share_count = inner
            .conn
            .query_row(
                "SELECT COUNT(*) FROM shares WHERE is_local = 1 AND active = 1",
                [],
                |row| row.get(0),
            )
            .unwrap_or(0);
        LibrarySummary {
            library_version,
            share_count,
            manifest_hash,
        }
    }

    pub fn settings(&self) -> LibrarySettings {
        let Ok(inner) = self.inner.lock() else {
            return default_library_settings();
        };
        inner
            .conn
            .query_row(
                "SELECT acceleration_enabled, max_upload_speed, max_upload_tasks, cache_limit_gb FROM settings WHERE id = 1",
                [],
                |row| {
                    Ok(LibrarySettings {
                        acceleration_enabled: row.get::<_, i64>(0)? != 0,
                        max_upload_speed: row.get(1)?,
                        max_upload_tasks: row.get(2)?,
                        cache_limit_gb: row.get(3)?,
                    })
                },
            )
            .unwrap_or_else(|_| default_library_settings())
    }

    pub fn update_settings(&self, settings: LibrarySettings) -> Result<LibrarySettings, String> {
        let inner = self.lock()?;
        let max_upload_tasks = settings.max_upload_tasks.clamp(1, 5);
        let cache_limit_gb = settings.cache_limit_gb.clamp(10, 500);
        inner
            .conn
            .execute(
                "UPDATE settings SET acceleration_enabled = ?1, max_upload_speed = ?2, max_upload_tasks = ?3, cache_limit_gb = ?4 WHERE id = 1",
                params![
                    if settings.acceleration_enabled { 1 } else { 0 },
                    normalize_speed(&settings.max_upload_speed),
                    max_upload_tasks,
                    cache_limit_gb
                ],
            )
            .map_err(|err| format!("保存共享设置失败: {err}"))?;
        drop(inner);
        Ok(self.settings())
    }

    pub fn add_share_paths(
        &self,
        paths: Vec<String>,
        category: String,
        permission: String,
        password: Option<String>,
    ) -> Result<Vec<ShareItem>, String> {
        let files = storage::collect_files(paths)?;
        if files.is_empty() {
            return Err("请选择至少一个文件或包含文件的文件夹".to_string());
        }

        let mut created = Vec::new();
        for path in files {
            created.push(self.add_one_share(&path, &category, &permission, password.clone())?);
        }
        self.bump_library_version()?;
        Ok(created)
    }

    pub fn update_share(&self, share_id: String, path: String) -> Result<ShareItem, String> {
        let path = PathBuf::from(path);
        if !path.is_file() {
            return Err("更新共享必须选择一个文件".to_string());
        }
        let now = now_secs();
        let name = storage::path_file_name(&path)?;
        let size = path
            .metadata()
            .map_err(|err| format!("读取文件信息失败: {err}"))?
            .len();
        let file_hash = sha256_path(&path)?;
        let inner = self.lock()?;
        let current: Option<(String, String, Option<String>, i64)> = inner
            .conn
            .query_row(
                "SELECT category, permission, password, COALESCE(MAX(version), 0)
                 FROM shares LEFT JOIN versions USING (share_id)
                 WHERE share_id = ?1 AND is_local = 1
                 GROUP BY shares.share_id",
                params![share_id],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
            )
            .optional()
            .map_err(|err| format!("查询共享失败: {err}"))?;
        let Some((_category, _permission, _password, current_version)) = current else {
            return Err("共享资源不存在".to_string());
        };
        let version = current_version + 1;
        let metadata = StoreMetadata {
            share_id: share_id.clone(),
            version,
            file_hash: file_hash.clone(),
            name: name.clone(),
            size,
            created_at: now,
        };
        storage::copy_to_shared_store(&path, &file_hash, &metadata)?;
        inner
            .conn
            .execute(
                "UPDATE shares SET name = ?1, updated_at = ?2, active = 1 WHERE share_id = ?3",
                params![name, now, share_id],
            )
            .map_err(|err| format!("更新共享失败: {err}"))?;
        inner
            .conn
            .execute(
                "INSERT OR REPLACE INTO versions (share_id, version, file_hash, size, created_at)
                 VALUES (?1, ?2, ?3, ?4, ?5)",
                params![share_id, version, file_hash, size as i64, now],
            )
            .map_err(|err| format!("写入版本失败: {err}"))?;
        self.insert_local_replica_locked(&inner, &share_id, version, &file_hash, size, now)?;
        drop(inner);
        self.bump_library_version()?;
        self.find_share(&share_id)
    }

    pub fn remove_share(&self, share_id: String) -> Result<(), String> {
        let inner = self.lock()?;
        let changed = inner
            .conn
            .execute(
                "UPDATE shares SET active = 0, updated_at = ?1 WHERE share_id = ?2 AND is_local = 1",
                params![now_secs(), share_id],
            )
            .map_err(|err| format!("取消共享失败: {err}"))?;
        if changed == 0 {
            return Err("共享资源不存在".to_string());
        }
        drop(inner);
        self.bump_library_version()
    }

    pub fn list_shared_resources(&self) -> Result<Vec<ShareItem>, String> {
        self.query_shares(false)
    }

    pub fn list_my_shares(&self) -> Result<Vec<ShareItem>, String> {
        self.query_shares(true)
    }

    pub fn local_manifest(&self) -> Result<Manifest, String> {
        let inner = self.lock()?;
        let shares = local_manifest_shares(&inner.conn)?;
        let library_version = inner
            .conn
            .query_row("SELECT version FROM library_state WHERE id = 1", [], |row| row.get(0))
            .unwrap_or(1);
        let hash = manifest_hash(&shares)?;
        Ok(Manifest {
            device_id: inner.device_id.clone(),
            device_name: inner.device_name.clone(),
            library_version,
            manifest_hash: hash,
            shares,
        })
    }

    pub fn observe_device(&self, packet: &DiscoveryPacket, ip: String) -> Result<bool, String> {
        let now = now_secs();
        let inner = self.lock()?;
        let previous: Option<(i64, String)> = inner
            .conn
            .query_row(
                "SELECT library_version, manifest_hash FROM devices WHERE device_id = ?1",
                params![packet.device_id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()
            .map_err(|err| format!("查询设备失败: {err}"))?;
        inner
            .conn
            .execute(
                "INSERT OR REPLACE INTO devices
                 (device_id, device_name, ip, tcp_port, api_port, library_version, share_count,
                  manifest_hash, upload_tasks, latency_ms, online, last_seen)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, NULL, 1, ?10)",
                params![
                    packet.device_id,
                    packet.device_name,
                    ip,
                    packet.tcp_port as i64,
                    packet.api_port as i64,
                    packet.library_version,
                    packet.share_count,
                    packet.manifest_hash,
                    packet.upload_tasks,
                    now
                ],
            )
            .map_err(|err| format!("保存设备失败: {err}"))?;
        Ok(previous
            .map(|(version, hash)| version != packet.library_version || hash != packet.manifest_hash)
            .unwrap_or(true))
    }

    pub fn has_active_remote_owner(&self, owner_device_id: &str) -> bool {
        let Ok(inner) = self.inner.lock() else {
            return false;
        };
        inner
            .conn
            .query_row(
                "SELECT EXISTS(
                    SELECT 1 FROM shares
                    WHERE owner_device_id = ?1 AND is_local = 0 AND active = 1
                )",
                params![owner_device_id],
                |row| row.get::<_, i64>(0),
            )
            .map(|exists| exists != 0)
            .unwrap_or(false)
    }

    pub fn merge_manifest(&self, manifest: Manifest, ip: String, tcp_port: u16, api_port: u16) -> Result<(), String> {
        let now = now_secs();
        let owner_device_id = manifest.device_id.clone();
        let owner_name = manifest.device_name.clone();
        let mut manifest_share_ids = HashSet::new();
        let inner = self.lock()?;
        for share in manifest.shares {
            manifest_share_ids.insert(share.share_id.clone());
            inner
                .conn
                .execute(
                    "INSERT OR REPLACE INTO shares
                     (share_id, name, category, permission, password, owner_device_id, owner_name,
                      active, is_local, created_at, updated_at, download_count)
                     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, 1, 0, ?8, ?9, ?10)",
                    params![
                        share.share_id,
                        share.name,
                        share.category,
                        share.permission,
                        share.password_hash,
                        share.owner_device_id,
                        share.owner_name,
                        share.created_at,
                        share.updated_at,
                        share.download_count
                    ],
                )
                .map_err(|err| format!("合并共享索引失败: {err}"))?;

            for version in share.versions {
                inner
                    .conn
                    .execute(
                        "INSERT OR REPLACE INTO versions
                         (share_id, version, file_hash, size, created_at)
                         VALUES (?1, ?2, ?3, ?4, ?5)",
                        params![
                            version.share_id,
                            version.version,
                            version.file_hash,
                            version.size as i64,
                            version.created_at
                        ],
                    )
                    .map_err(|err| format!("合并版本失败: {err}"))?;
                inner
                    .conn
                    .execute(
                        "INSERT OR REPLACE INTO replicas
                         (share_id, version, file_hash, device_id, device_name, ip, tcp_port, api_port,
                          online, upload_tasks, latency_ms, is_local, last_seen)
                         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, 1, 0, NULL, 0, ?9)",
                        params![
                            version.share_id,
                            version.version,
                            version.file_hash,
                            owner_device_id,
                            owner_name,
                            ip,
                            tcp_port as i64,
                            api_port as i64,
                            now
                        ],
                    )
                    .map_err(|err| format!("合并副本节点失败: {err}"))?;
            }
        }

        let mut stmt = inner
            .conn
            .prepare(
                "SELECT share_id FROM shares
                 WHERE owner_device_id = ?1 AND is_local = 0 AND active = 1",
            )
            .map_err(|err| format!("鏌ヨ杩滅▼鍏变韩澶辫触: {err}"))?;
        let active_share_ids = stmt
            .query_map(params![owner_device_id], |row| row.get::<_, String>(0))
            .map_err(|err| format!("鏌ヨ杩滅▼鍏变韩澶辫触: {err}"))?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|err| format!("璇诲彇杩滅▼鍏变韩澶辫触: {err}"))?;
        drop(stmt);

        for share_id in active_share_ids {
            if manifest_share_ids.contains(&share_id) {
                continue;
            }
            inner
                .conn
                .execute(
                    "UPDATE shares
                     SET active = 0, updated_at = ?1
                     WHERE share_id = ?2 AND owner_device_id = ?3 AND is_local = 0",
                    params![now, share_id, owner_device_id],
                )
                .map_err(|err| format!("鍙栨秷杩滅▼鍏变韩绱㈠紩澶辫触: {err}"))?;
        }

        Ok(())
    }

    pub fn select_download_source(&self, share_id: &str) -> Result<DownloadSource, String> {
        let inner = self.lock()?;
        inner
            .conn
            .query_row(
                "
                SELECT s.share_id, v.version, v.file_hash, s.name, v.size,
                       r.device_id, r.device_name, r.ip, r.tcp_port, r.api_port, r.is_local
                FROM shares s
                JOIN versions v ON v.share_id = s.share_id
                JOIN replicas r ON r.share_id = v.share_id
                    AND r.version = v.version
                    AND r.file_hash = v.file_hash
                WHERE s.share_id = ?1 AND s.active = 1 AND r.online = 1
                ORDER BY v.version DESC, r.upload_tasks ASC, COALESCE(r.latency_ms, 999999) ASC, r.is_local DESC
                LIMIT 1
                ",
                params![share_id],
                |row| {
                    Ok(DownloadSource {
                        share_id: row.get(0)?,
                        version: row.get(1)?,
                        file_hash: row.get(2)?,
                        name: row.get(3)?,
                        size: row.get::<_, i64>(4)? as u64,
                        device_id: row.get(5)?,
                        device_name: row.get(6)?,
                        ip: row.get(7)?,
                        tcp_port: row.get::<_, i64>(8)? as u16,
                        api_port: row.get::<_, i64>(9)? as u16,
                        is_local: row.get::<_, i64>(10)? != 0,
                    })
                },
            )
            .optional()
            .map_err(|err| format!("选择下载源失败: {err}"))?
            .ok_or_else(|| "当前没有可用的在线副本节点".to_string())
    }

    pub fn verify_share_password(
        &self,
        share_id: &str,
        password: Option<&str>,
    ) -> Result<(), String> {
        let inner = self.lock()?;
        let row: Option<(String, Option<String>)> = inner
            .conn
            .query_row(
                "SELECT permission, password FROM shares WHERE share_id = ?1 AND active = 1",
                params![share_id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()
            .map_err(|err| format!("查询共享密码失败: {err}"))?;
        let Some((permission, stored)) = row else {
            return Err("共享资源不存在".to_string());
        };
        if permission != "password" {
            return Ok(());
        }
        let Some(stored) = stored.filter(|value| !value.trim().is_empty()) else {
            return Err("密码错误".to_string());
        };
        if password_matches(password.unwrap_or_default(), &stored) {
            Ok(())
        } else {
            Err("密码错误".to_string())
        }
    }

    pub fn shared_content(
        &self,
        share_id: &str,
        version: i64,
        file_hash: &str,
        password: Option<&str>,
    ) -> Result<SharedContent, String> {
        self.verify_share_password(share_id, password)?;
        let inner = self.lock()?;
        let content = inner
            .conn
            .query_row(
                "
                SELECT s.name, v.size
                FROM shares s
                JOIN versions v ON v.share_id = s.share_id
                JOIN replicas r ON r.share_id = v.share_id
                    AND r.version = v.version
                    AND r.file_hash = v.file_hash
                WHERE s.share_id = ?1 AND v.version = ?2 AND v.file_hash = ?3
                  AND r.device_id = ?4 AND r.is_local = 1
                LIMIT 1
                ",
                params![share_id, version, file_hash, inner.device_id],
                |row| {
                    Ok(SharedContent {
                        share_id: share_id.to_string(),
                        version,
                        file_hash: file_hash.to_string(),
                        name: row.get(0)?,
                        size: row.get::<_, i64>(1)? as u64,
                        path: storage::shared_content_path(file_hash),
                    })
                },
            )
            .optional()
            .map_err(|err| format!("查询共享内容失败: {err}"))?
            .ok_or_else(|| "本机没有该资源副本".to_string())?;
        let path = storage::validate_shared_content(file_hash)?;
        Ok(SharedContent { path, ..content })
    }

    pub fn register_local_replica(
        &self,
        share_id: &str,
        version: i64,
        file_hash: &str,
        name: &str,
        size: u64,
        source_path: &Path,
    ) -> Result<(), String> {
        let now = now_secs();
        let metadata = StoreMetadata {
            share_id: share_id.to_string(),
            version,
            file_hash: file_hash.to_string(),
            name: name.to_string(),
            size,
            created_at: now,
        };
        storage::copy_to_shared_store(source_path, file_hash, &metadata)?;
        let inner = self.lock()?;
        let exists: bool = inner
            .conn
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM shares WHERE share_id = ?1)",
                params![share_id],
                |row| row.get::<_, i64>(0),
            )
            .unwrap_or(0)
            != 0;
        if !exists {
            inner
                .conn
                .execute(
                    "INSERT INTO shares
                     (share_id, name, category, permission, password, owner_device_id, owner_name,
                      active, is_local, created_at, updated_at, download_count)
                     VALUES (?1, ?2, '其他', 'public', NULL, ?3, ?4, 1, 0, ?5, ?5, 0)",
                    params![share_id, name, inner.device_id, inner.device_name, now],
                )
                .map_err(|err| format!("登记下载资源失败: {err}"))?;
        }
        inner
            .conn
            .execute(
                "INSERT OR REPLACE INTO versions (share_id, version, file_hash, size, created_at)
                 VALUES (?1, ?2, ?3, ?4, ?5)",
                params![share_id, version, file_hash, size as i64, now],
            )
            .map_err(|err| format!("登记下载版本失败: {err}"))?;
        self.insert_local_replica_locked(&inner, share_id, version, file_hash, size, now)?;
        drop(inner);
        self.bump_library_version()
    }

    pub fn increment_download_count(&self, share_id: &str) {
        if let Ok(inner) = self.inner.lock() {
            let _ = inner.conn.execute(
                "UPDATE shares SET download_count = download_count + 1 WHERE share_id = ?1",
                params![share_id],
            );
        }
    }

    fn add_one_share(
        &self,
        path: &Path,
        category: &str,
        permission: &str,
        password: Option<String>,
    ) -> Result<ShareItem, String> {
        let now = now_secs();
        let share_id = Uuid::new_v4().to_string();
        let version = 1;
        let name = storage::path_file_name(path)?;
        let size = path
            .metadata()
            .map_err(|err| format!("读取文件信息失败: {err}"))?
            .len();
        let file_hash = sha256_path(path)?;
        let metadata = StoreMetadata {
            share_id: share_id.clone(),
            version,
            file_hash: file_hash.clone(),
            name: name.clone(),
            size,
            created_at: now,
        };
        storage::copy_to_shared_store(path, &file_hash, &metadata)?;
        let inner = self.lock()?;
        inner
            .conn
            .execute(
                "INSERT INTO shares
                 (share_id, name, category, permission, password, owner_device_id, owner_name,
                  active, is_local, created_at, updated_at, download_count)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, 1, 1, ?8, ?8, 0)",
                params![
                    share_id,
                    name,
                    clean_category(category),
                    clean_permission(permission),
                    normalized_password_for_storage(permission, password.as_deref())?,
                    inner.device_id,
                    inner.device_name,
                    now
                ],
            )
            .map_err(|err| format!("创建共享索引失败: {err}"))?;
        inner
            .conn
            .execute(
                "INSERT INTO versions (share_id, version, file_hash, size, created_at)
                 VALUES (?1, ?2, ?3, ?4, ?5)",
                params![share_id, version, file_hash, size as i64, now],
            )
            .map_err(|err| format!("创建共享版本失败: {err}"))?;
        self.insert_local_replica_locked(&inner, &share_id, version, &file_hash, size, now)?;
        drop(inner);
        self.find_share(&share_id)
    }

    fn insert_local_replica_locked(
        &self,
        inner: &LibraryInner,
        share_id: &str,
        version: i64,
        file_hash: &str,
        _size: u64,
        now: i64,
    ) -> Result<(), String> {
        inner
            .conn
            .execute(
                "INSERT OR REPLACE INTO replicas
                 (share_id, version, file_hash, device_id, device_name, ip, tcp_port, api_port,
                  online, upload_tasks, latency_ms, is_local, last_seen)
                 VALUES (?1, ?2, ?3, ?4, ?5, '127.0.0.1', ?6, ?7, 1, 0, 0, 1, ?8)",
                params![
                    share_id,
                    version,
                    file_hash,
                    inner.device_id,
                    inner.device_name,
                    TCP_PORT as i64,
                    crate::protocol::LAN_API_PORT as i64,
                    now
                ],
            )
            .map_err(|err| format!("登记本机副本失败: {err}"))?;
        Ok(())
    }

    fn bump_library_version(&self) -> Result<(), String> {
        let inner = self.lock()?;
        inner
            .conn
            .execute(
                "UPDATE library_state SET version = version + 1 WHERE id = 1",
                [],
            )
            .map_err(|err| format!("更新资源库版本失败: {err}"))?;
        Ok(())
    }

    fn find_share(&self, share_id: &str) -> Result<ShareItem, String> {
        self.query_share(Some(share_id), false)?
            .into_iter()
            .next()
            .ok_or_else(|| "共享资源不存在".to_string())
    }

    fn query_shares(&self, only_local: bool) -> Result<Vec<ShareItem>, String> {
        self.query_share(None, only_local)
    }

    fn query_share(&self, share_id: Option<&str>, only_local: bool) -> Result<Vec<ShareItem>, String> {
        let inner = self.lock()?;
        let mut sql = String::from(
            "
            SELECT s.share_id, s.name, s.category, s.permission, s.owner_device_id, s.owner_name,
                   v.version, v.file_hash, v.size, s.created_at, s.updated_at, s.download_count,
                   COUNT(DISTINCT r.device_id) AS replica_count, s.is_local, s.active
            FROM shares s
            JOIN versions v ON v.share_id = s.share_id
            LEFT JOIN replicas r ON r.share_id = v.share_id
                AND r.version = v.version
                AND r.file_hash = v.file_hash
                AND r.online = 1
            WHERE s.active = 1
              AND v.version = (SELECT MAX(version) FROM versions WHERE share_id = s.share_id)
            ",
        );
        if only_local {
            sql.push_str(" AND s.is_local = 1");
        }
        if share_id.is_some() {
            sql.push_str(" AND s.share_id = ?1");
        }
        sql.push_str(" GROUP BY s.share_id ORDER BY s.updated_at DESC, s.name ASC");

        let mut stmt = inner
            .conn
            .prepare(&sql)
            .map_err(|err| format!("查询共享列表失败: {err}"))?;
        let mapper = |row: &rusqlite::Row<'_>| {
            Ok(ShareItem {
                share_id: row.get(0)?,
                name: row.get(1)?,
                category: row.get(2)?,
                permission: row.get(3)?,
                owner_device_id: row.get(4)?,
                owner_name: row.get(5)?,
                latest_version: row.get(6)?,
                file_hash: row.get(7)?,
                size: row.get::<_, i64>(8)? as u64,
                created_at: row.get(9)?,
                updated_at: row.get(10)?,
                download_count: row.get(11)?,
                replica_count: row.get(12)?,
                is_local: row.get::<_, i64>(13)? != 0,
                active: row.get::<_, i64>(14)? != 0,
            })
        };
        let rows = if let Some(share_id) = share_id {
            stmt.query_map(params![share_id], mapper)
        } else {
            stmt.query_map([], mapper)
        }
        .map_err(|err| format!("读取共享列表失败: {err}"))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|err| format!("解析共享列表失败: {err}"))
    }

    fn lock(&self) -> Result<std::sync::MutexGuard<'_, LibraryInner>, String> {
        self.inner
            .lock()
            .map_err(|_| "资源库正在被占用".to_string())
    }
}

fn local_manifest_shares(conn: &Connection) -> Result<Vec<ManifestShare>, String> {
    let mut stmt = conn
        .prepare(
            "
            SELECT share_id, name, category, permission, password, owner_device_id, owner_name,
                   created_at, updated_at, download_count
            FROM shares
            WHERE is_local = 1 AND active = 1
            ORDER BY share_id ASC
            ",
        )
        .map_err(|err| format!("生成 manifest 失败: {err}"))?;
    let rows = stmt
        .query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, String>(3)?,
                row.get::<_, Option<String>>(4)?,
                row.get::<_, String>(5)?,
                row.get::<_, String>(6)?,
                row.get::<_, i64>(7)?,
                row.get::<_, i64>(8)?,
                row.get::<_, i64>(9)?,
            ))
        })
        .map_err(|err| format!("读取 manifest 失败: {err}"))?;

    let mut shares = Vec::new();
    for row in rows {
        let (share_id, name, category, permission, password_hash, owner_device_id, owner_name, created_at, updated_at, download_count) =
            row.map_err(|err| format!("解析 manifest 失败: {err}"))?;
        let versions = versions_for(conn, &share_id)?;
        let latest_version = versions.iter().map(|item| item.version).max().unwrap_or(1);
        shares.push(ManifestShare {
            share_id,
            name,
            category,
            permission,
            password_hash,
            owner_device_id,
            owner_name,
            latest_version,
            versions,
            download_count,
            created_at,
            updated_at,
        });
    }
    Ok(shares)
}

fn versions_for(conn: &Connection, share_id: &str) -> Result<Vec<ShareVersion>, String> {
    let mut stmt = conn
        .prepare(
            "SELECT share_id, version, file_hash, size, created_at
             FROM versions WHERE share_id = ?1 ORDER BY version ASC",
        )
        .map_err(|err| format!("读取版本失败: {err}"))?;
    let rows = stmt
        .query_map(params![share_id], |row| {
            Ok(ShareVersion {
                share_id: row.get(0)?,
                version: row.get(1)?,
                file_hash: row.get(2)?,
                size: row.get::<_, i64>(3)? as u64,
                created_at: row.get(4)?,
            })
        })
        .map_err(|err| format!("查询版本失败: {err}"))?;
    rows.collect::<Result<Vec<_>, _>>()
        .map_err(|err| format!("解析版本失败: {err}"))
}

fn manifest_hash(shares: &[ManifestShare]) -> Result<String, String> {
    let payload = serde_json::to_vec(shares).map_err(|err| format!("生成 manifest hash 失败: {err}"))?;
    let mut hasher = Sha256::new();
    hasher.update(payload);
    Ok(format!("{:x}", hasher.finalize()))
}

pub fn sha256_path(path: &Path) -> Result<String, String> {
    let mut file = File::open(path).map_err(|err| format!("打开文件失败 {}: {err}", path.display()))?;
    let mut hasher = Sha256::new();
    let mut buf = vec![0_u8; crate::protocol::CHUNK_SIZE];
    loop {
        let read = file
            .read(&mut buf)
            .map_err(|err| format!("计算 SHA256 失败: {err}"))?;
        if read == 0 {
            break;
        }
        hasher.update(&buf[..read]);
    }
    Ok(format!("{:x}", hasher.finalize()))
}

fn now_secs() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs() as i64)
        .unwrap_or_default()
}

fn clean_category(value: &str) -> String {
    let value = value.trim();
    if value.is_empty() {
        "其他".to_string()
    } else {
        value.chars().take(32).collect()
    }
}

fn clean_permission(value: &str) -> String {
    match value {
        "password" => "password".to_string(),
        _ => "public".to_string(),
    }
}

fn clean_device_note(value: &str) -> String {
    value.trim().chars().take(48).collect()
}

fn normalized_password_for_storage(permission: &str, password: Option<&str>) -> Result<Option<String>, String> {
    if clean_permission(permission) != "password" {
        return Ok(None);
    }
    let password = password.unwrap_or_default().trim();
    if password.is_empty() {
        return Err("密码共享必须设置访问密码".to_string());
    }
    Ok(Some(format!("sha256:{}", sha256_text(password))))
}

fn password_matches(input: &str, stored: &str) -> bool {
    let input = input.trim();
    if let Some(hash) = stored.strip_prefix("sha256:") {
        sha256_text(input) == hash
    } else {
        input == stored
    }
}

fn sha256_text(value: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(value.as_bytes());
    format!("{:x}", hasher.finalize())
}

fn normalize_speed(value: &str) -> String {
    match value {
        "10MB/s" | "50MB/s" | "100MB/s" => value.to_string(),
        _ => "unlimited".to_string(),
    }
}

fn default_library_settings() -> LibrarySettings {
    LibrarySettings {
        acceleration_enabled: true,
        max_upload_speed: "unlimited".to_string(),
        max_upload_tasks: 3,
        cache_limit_gb: 50,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn manifest_hash_is_stable() {
        let shares = vec![ManifestShare {
            share_id: "s1".to_string(),
            name: "a.txt".to_string(),
            category: "文档".to_string(),
            permission: "public".to_string(),
            password_hash: None,
            owner_device_id: "d1".to_string(),
            owner_name: "pc".to_string(),
            latest_version: 1,
            versions: vec![ShareVersion {
                share_id: "s1".to_string(),
                version: 1,
                file_hash: "abc".to_string(),
                size: 3,
                created_at: 1,
            }],
            download_count: 0,
            created_at: 1,
            updated_at: 1,
        }];
        assert_eq!(manifest_hash(&shares).unwrap(), manifest_hash(&shares).unwrap());
    }
}
