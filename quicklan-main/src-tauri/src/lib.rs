mod commands;
mod control_api;
mod discovery;
mod lan_api;
mod library;
mod protocol;
mod settings;
mod storage;
mod transfer;

use commands::{
    accept_transfer, add_share_paths, choose_download_dir, choose_folder_path, choose_share_paths,
    clear_finished_transfers, discover_ip, download_share, get_app_info, get_control_api_info,
    get_library_settings, get_network_status, get_settings,
    get_transfer, get_transfers, list_devices, list_my_shares, list_shared_resources,
    open_path_location, reject_transfer, remove_share, remove_transfer_record, send_files,
    update_library_settings, update_nickname, update_share,
    update_device_note,
};
use discovery::DiscoveryService;
use library::LibraryService;
use protocol::LAN_API_PORT;
use serde::Serialize;
use std::{
    io::{Read, Write},
    net::TcpStream as StdTcpStream,
    time::Duration,
};
use settings::SettingsService;
use tauri::{
    menu::MenuBuilder,
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Manager, WindowEvent,
};
use transfer::TransferService;

const CONTROL_API_BIND: &str = "127.0.0.1:45456";

#[derive(Clone, Serialize)]
pub struct ControlApiInfo {
    pub enabled: bool,
    pub bind: String,
}

#[derive(Clone, Serialize)]
pub struct AppInfo {
    pub version: &'static str,
}

pub struct AppState {
    pub discovery: DiscoveryService,
    pub transfer: TransferService,
    pub settings: SettingsService,
    pub library: LibraryService,
    pub control_api: ControlApiInfo,
}

pub(crate) fn show_main_window(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

fn setup_tray(app: &tauri::App) -> tauri::Result<()> {
    let menu = MenuBuilder::new(app)
        .text("show", "打开 QuickLAN")
        .separator()
        .text("quit", "退出")
        .build()?;
    let mut builder = TrayIconBuilder::with_id("quicklan-tray")
        .tooltip("QuickLAN")
        .menu(&menu)
        .show_menu_on_left_click(false);
    if let Some(icon) = app.default_window_icon().cloned() {
        builder = builder.icon(icon);
    }
    builder.build(app)?;
    Ok(())
}

#[cfg(windows)]
struct SingleInstanceGuard(windows_sys::Win32::Foundation::HANDLE);

#[cfg(windows)]
impl Drop for SingleInstanceGuard {
    fn drop(&mut self) {
        unsafe {
            let _ = windows_sys::Win32::Foundation::CloseHandle(self.0);
        }
    }
}

#[cfg(windows)]
fn acquire_single_instance() -> Option<SingleInstanceGuard> {
    use windows_sys::Win32::{
        Foundation::{GetLastError, ERROR_ALREADY_EXISTS},
        System::Threading::CreateMutexW,
    };

    let name = "Global\\QuickLAN.SingleInstance"
        .encode_utf16()
        .chain(std::iter::once(0))
        .collect::<Vec<_>>();
    let handle = unsafe { CreateMutexW(std::ptr::null(), 1, name.as_ptr()) };
    if handle.is_null() {
        eprintln!("failed to create QuickLAN single-instance mutex");
        return None;
    }
    if unsafe { GetLastError() } == ERROR_ALREADY_EXISTS {
        unsafe {
            let _ = windows_sys::Win32::Foundation::CloseHandle(handle);
        }
        notify_existing_instance();
        return None;
    }
    Some(SingleInstanceGuard(handle))
}

#[cfg(not(windows))]
fn acquire_single_instance() -> Option<()> {
    Some(())
}

fn notify_existing_instance() {
    let Ok(mut stream) = StdTcpStream::connect(CONTROL_API_BIND) else {
        return;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(500)));
    let _ = stream.write_all(
        b"POST /show HTTP/1.1\r\nHost: localhost\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
    );
    let mut buf = [0_u8; 128];
    let _ = stream.read(&mut buf);
}

pub fn run() {
    let Some(_single_instance_guard) = acquire_single_instance() else {
        return;
    };

    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            setup_tray(app)?;
            let app_handle = app.handle().clone();
            let settings = SettingsService::load();
            let device_id = discovery::load_or_create_device_id();
            let library = LibraryService::load(device_id, settings.nickname())
                .map_err(|err| format!("failed to load library: {err}"))?;
            let api_port = lan_api::start(library.clone(), LAN_API_PORT);
            let transfer = TransferService::new(
                app_handle.clone(),
                settings.clone(),
                library.clone(),
                api_port,
            );
            let tcp_port = transfer
                .start_listener()
                .map_err(|err| format!("failed to start TCP listener: {err}"))?;
            let discovery = DiscoveryService::new(
                app_handle.clone(),
                tcp_port,
                api_port,
                settings.clone(),
                library.clone(),
            );

            discovery.start();

            let control_api = ControlApiInfo {
                enabled: true,
                bind: CONTROL_API_BIND.to_string(),
            };

            let state = AppState {
                discovery,
                transfer,
                settings,
                library,
                control_api: control_api.clone(),
            };
            app.manage(state);
            control_api::start(app_handle, control_api.bind.clone());
            Ok(())
        })
        .on_menu_event(|app, event| match event.id().as_ref() {
            "show" => show_main_window(app),
            "quit" => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if matches!(
                event,
                TrayIconEvent::DoubleClick {
                    button: MouseButton::Left,
                    ..
                } | TrayIconEvent::Click {
                    button: MouseButton::Left,
                    button_state: MouseButtonState::Up,
                    ..
                }
            ) {
                show_main_window(tray.app_handle());
            }
        })
        .on_window_event(|window, event| {
            if window.label() != "main" {
                return;
            }
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .invoke_handler(tauri::generate_handler![
            list_devices,
            update_device_note,
            send_files,
            accept_transfer,
            reject_transfer,
            get_transfers,
            get_transfer,
            remove_transfer_record,
            clear_finished_transfers,
            get_app_info,
            get_control_api_info,
            discover_ip,
            get_network_status,
            get_settings,
            update_nickname,
            choose_download_dir,
            choose_share_paths,
            choose_folder_path,
            open_path_location,
            list_shared_resources,
            list_my_shares,
            add_share_paths,
            update_share,
            remove_share,
            download_share,
            get_library_settings,
            update_library_settings
        ])
        .run(tauri::generate_context!())
        .expect("failed to run QuickLAN");
}
