use crate::storage;
use serde::{Deserialize, Serialize};
use std::{
    fs,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AppSettings {
    pub nickname: String,
    pub download_dir: String,
}

#[derive(Clone)]
pub struct SettingsService {
    inner: Arc<Mutex<AppSettings>>,
    path: PathBuf,
}

impl SettingsService {
    pub fn load() -> Self {
        let path = settings_path();
        let defaults = default_settings();
        let settings = fs::read_to_string(&path)
            .ok()
            .and_then(|content| serde_json::from_str::<AppSettings>(&content).ok())
            .map(|settings| normalize_settings(settings, &defaults))
            .unwrap_or(defaults);

        let service = Self {
            inner: Arc::new(Mutex::new(settings)),
            path,
        };
        let _ = service.save();
        service
    }

    pub fn get(&self) -> AppSettings {
        self.inner
            .lock()
            .map(|settings| settings.clone())
            .unwrap_or_else(|_| default_settings())
    }

    pub fn nickname(&self) -> String {
        self.get().nickname
    }

    pub fn download_dir(&self) -> PathBuf {
        PathBuf::from(self.get().download_dir)
    }

    pub fn update_nickname(&self, nickname: String) -> Result<AppSettings, String> {
        let nickname = clean_nickname(&nickname);
        if nickname.is_empty() {
            return Err("昵称不能为空".to_string());
        }
        {
            let mut settings = self
                .inner
                .lock()
                .map_err(|_| "设置正在被占用".to_string())?;
            settings.nickname = nickname;
        }
        self.save()?;
        Ok(self.get())
    }

    pub fn update_download_dir(&self, dir: impl AsRef<Path>) -> Result<AppSettings, String> {
        let dir = dir.as_ref();
        fs::create_dir_all(dir).map_err(|err| format!("创建保存目录失败: {err}"))?;
        {
            let mut settings = self
                .inner
                .lock()
                .map_err(|_| "设置正在被占用".to_string())?;
            settings.download_dir = dir.display().to_string();
        }
        self.save()?;
        Ok(self.get())
    }

    fn save(&self) -> Result<(), String> {
        if let Some(parent) = self.path.parent() {
            fs::create_dir_all(parent).map_err(|err| format!("创建配置目录失败: {err}"))?;
        }
        let settings = self.get();
        let content = serde_json::to_string_pretty(&settings)
            .map_err(|err| format!("序列化设置失败: {err}"))?;
        fs::write(&self.path, content).map_err(|err| format!("保存设置失败: {err}"))
    }
}

fn settings_path() -> PathBuf {
    storage::config_dir().join("settings.json")
}

fn default_settings() -> AppSettings {
    AppSettings {
        nickname: default_nickname(),
        download_dir: default_download_dir().display().to_string(),
    }
}

fn normalize_settings(mut settings: AppSettings, defaults: &AppSettings) -> AppSettings {
    settings.nickname = clean_nickname(&settings.nickname);
    if settings.nickname.is_empty() {
        settings.nickname = defaults.nickname.clone();
    }
    if settings.download_dir.trim().is_empty() {
        settings.download_dir = defaults.download_dir.clone();
    }
    settings
}

fn default_nickname() -> String {
    hostname::get()
        .ok()
        .and_then(|value| value.into_string().ok())
        .filter(|value| !value.trim().is_empty())
        .unwrap_or_else(|| "我的电脑".to_string())
}

fn default_download_dir() -> PathBuf {
    dirs::download_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join("QuickLAN")
}

fn clean_nickname(value: &str) -> String {
    value.trim().chars().take(32).collect()
}
