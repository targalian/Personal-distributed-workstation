use crate::{
    protocol::{DeviceInfo, LibrarySettings, NetworkStatus, ShareItem, TransferInfo},
    settings::AppSettings,
    storage,
    AppInfo, AppState, ControlApiInfo,
};
use std::{path::PathBuf, process::Command};
use tauri::{AppHandle, State};
use tauri_plugin_dialog::DialogExt;

#[tauri::command]
pub fn list_devices(state: State<'_, AppState>) -> Vec<DeviceInfo> {
    state.discovery.list_devices()
}

#[tauri::command]
pub fn update_device_note(
    state: State<'_, AppState>,
    device_id: String,
    note: String,
) -> Result<Vec<DeviceInfo>, String> {
    state.discovery.update_device_note(device_id, note)
}

#[tauri::command]
pub fn send_files(
    state: State<'_, AppState>,
    target_id: String,
    file_paths: Vec<String>,
) -> Result<String, String> {
    let file_paths = storage::collect_files(file_paths)?
        .into_iter()
        .map(|path| path.display().to_string())
        .collect::<Vec<_>>();
    let target = state
        .discovery
        .find_device(&target_id)
        .ok_or_else(|| "目标设备不在线".to_string())?;
    state.transfer.send_files(
        target.ip,
        target.tcp_port,
        target.name,
        state.discovery.local_sender(),
        file_paths,
    )
}

#[tauri::command]
pub fn discover_ip(state: State<'_, AppState>, ip: String) -> Result<(), String> {
    state.discovery.probe_ip(ip)
}

#[tauri::command]
pub fn accept_transfer(state: State<'_, AppState>, transfer_id: String) -> Result<(), String> {
    state.transfer.accept(&transfer_id)
}

#[tauri::command]
pub fn reject_transfer(state: State<'_, AppState>, transfer_id: String) -> Result<(), String> {
    state.transfer.reject(&transfer_id)
}

#[tauri::command]
pub fn get_transfers(state: State<'_, AppState>) -> Vec<TransferInfo> {
    state.transfer.list_transfers()
}

#[tauri::command]
pub fn get_transfer(state: State<'_, AppState>, transfer_id: String) -> Option<TransferInfo> {
    state.transfer.get_transfer(&transfer_id)
}

#[tauri::command]
pub fn remove_transfer_record(state: State<'_, AppState>, transfer_id: String) -> Result<(), String> {
    state.transfer.remove_transfer(&transfer_id)
}

#[tauri::command]
pub fn clear_finished_transfers(state: State<'_, AppState>) -> Result<(), String> {
    state.transfer.clear_finished()
}

#[tauri::command]
pub fn get_app_info() -> AppInfo {
    AppInfo {
        version: env!("CARGO_PKG_VERSION"),
    }
}

#[tauri::command]
pub fn get_control_api_info(state: State<'_, AppState>) -> ControlApiInfo {
    state.control_api.clone()
}

#[tauri::command]
pub fn get_network_status(state: State<'_, AppState>) -> NetworkStatus {
    state.discovery.network_status()
}

#[tauri::command]
pub fn get_settings(state: State<'_, AppState>) -> AppSettings {
    state.settings.get()
}

#[tauri::command]
pub fn update_nickname(
    state: State<'_, AppState>,
    nickname: String,
) -> Result<AppSettings, String> {
    let next = state.settings.update_nickname(nickname)?;
    state.library.set_device_name(next.nickname.clone());
    state.discovery.broadcast_now();
    Ok(next)
}

#[tauri::command]
pub async fn choose_download_dir(
    app: AppHandle,
    state: State<'_, AppState>,
) -> Result<Option<AppSettings>, String> {
    let Some(folder) = app.dialog().file().blocking_pick_folder() else {
        return Ok(None);
    };
    let path = folder
        .into_path()
        .map_err(|_| "请选择本地文件夹路径".to_string())?;
    state.settings.update_download_dir(path).map(Some)
}

#[tauri::command]
pub async fn choose_share_paths(app: AppHandle) -> Result<Vec<String>, String> {
    let Some(files) = app.dialog().file().blocking_pick_files() else {
        return Ok(Vec::new());
    };
    files
        .into_iter()
        .map(|file| {
            file.into_path()
                .map(|path| path.display().to_string())
                .map_err(|_| "请选择本地文件路径".to_string())
        })
        .collect()
}

#[tauri::command]
pub async fn choose_folder_path(app: AppHandle) -> Result<Option<String>, String> {
    let Some(folder) = app.dialog().file().blocking_pick_folder() else {
        return Ok(None);
    };
    folder
        .into_path()
        .map(|path| Some(path.display().to_string()))
        .map_err(|_| "请选择本地文件夹路径".to_string())
}

#[tauri::command]
pub fn open_path_location(path: String) -> Result<(), String> {
    let path = PathBuf::from(path);
    if path.is_file() {
        let target = path.canonicalize().unwrap_or(path);
        Command::new("explorer.exe")
            .arg(format!("/select,{}", target.display()))
            .spawn()
            .map_err(|err| format!("打开资源管理器失败: {err}"))?;
    } else {
        let mut target = if path.is_dir() {
            path
        } else {
            path.parent()
                .map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from("."))
        };
        while !target.exists() {
            let Some(parent) = target.parent().map(PathBuf::from) else {
                target = PathBuf::from(".");
                break;
            };
            target = parent;
        }
        let target = target.canonicalize().unwrap_or(target);
        Command::new("explorer.exe")
            .arg(target)
            .spawn()
            .map_err(|err| format!("打开资源管理器失败: {err}"))?;
    }
    Ok(())
}

#[tauri::command]
pub fn list_shared_resources(state: State<'_, AppState>) -> Result<Vec<ShareItem>, String> {
    state.library.list_shared_resources()
}

#[tauri::command]
pub fn list_my_shares(state: State<'_, AppState>) -> Result<Vec<ShareItem>, String> {
    state.library.list_my_shares()
}

#[tauri::command]
pub fn add_share_paths(
    state: State<'_, AppState>,
    paths: Vec<String>,
    category: String,
    permission: String,
    password: Option<String>,
) -> Result<Vec<ShareItem>, String> {
    let shares = state
        .library
        .add_share_paths(paths, category, permission, password)?;
    state.discovery.broadcast_now();
    Ok(shares)
}

#[tauri::command]
pub fn update_share(
    state: State<'_, AppState>,
    share_id: String,
    path: String,
) -> Result<ShareItem, String> {
    let share = state.library.update_share(share_id, path)?;
    state.discovery.broadcast_now();
    Ok(share)
}

#[tauri::command]
pub fn remove_share(state: State<'_, AppState>, share_id: String) -> Result<(), String> {
    state.library.remove_share(share_id)?;
    state.discovery.broadcast_now();
    Ok(())
}

#[tauri::command]
pub fn download_share(
    state: State<'_, AppState>,
    share_id: String,
    password: Option<String>,
) -> Result<String, String> {
    state
        .library
        .verify_share_password(&share_id, password.as_deref())?;
    let source = state.library.select_download_source(&share_id)?;
    state
        .transfer
        .download_shared(source, state.discovery.local_sender(), password)
}

#[tauri::command]
pub fn get_library_settings(state: State<'_, AppState>) -> LibrarySettings {
    state.library.settings()
}

#[tauri::command]
pub fn update_library_settings(
    state: State<'_, AppState>,
    settings: LibrarySettings,
) -> Result<LibrarySettings, String> {
    state.library.update_settings(settings)
}
