import { invoke } from "@tauri-apps/api/core";
import type {
  AppInfo,
  AppSettings,
  ControlApiInfo,
  DeviceInfo,
  LibrarySettings,
  NetworkStatus,
  ShareItem,
  TransferInfo,
} from "./types";

export function listDevices(): Promise<DeviceInfo[]> {
  return invoke<DeviceInfo[]>("list_devices");
}

export function updateDeviceNote(deviceId: string, note: string): Promise<DeviceInfo[]> {
  return invoke<DeviceInfo[]>("update_device_note", { deviceId, note });
}

export function sendFiles(targetId: string, filePaths: string[]): Promise<string> {
  return invoke<string>("send_files", { targetId, filePaths });
}

export function acceptTransfer(transferId: string): Promise<void> {
  return invoke<void>("accept_transfer", { transferId });
}

export function rejectTransfer(transferId: string): Promise<void> {
  return invoke<void>("reject_transfer", { transferId });
}

export function getTransfers(): Promise<TransferInfo[]> {
  return invoke<TransferInfo[]>("get_transfers");
}

export function getTransfer(transferId: string): Promise<TransferInfo | null> {
  return invoke<TransferInfo | null>("get_transfer", { transferId });
}

export function getControlApiInfo(): Promise<ControlApiInfo> {
  return invoke<ControlApiInfo>("get_control_api_info");
}

export function getAppInfo(): Promise<AppInfo> {
  return invoke<AppInfo>("get_app_info");
}

export function discoverIp(ip: string): Promise<void> {
  return invoke<void>("discover_ip", { ip });
}

export function getNetworkStatus(): Promise<NetworkStatus> {
  return invoke<NetworkStatus>("get_network_status");
}

export function getSettings(): Promise<AppSettings> {
  return invoke<AppSettings>("get_settings");
}

export function updateNickname(nickname: string): Promise<AppSettings> {
  return invoke<AppSettings>("update_nickname", { nickname });
}

export function chooseDownloadDir(): Promise<AppSettings | null> {
  return invoke<AppSettings | null>("choose_download_dir");
}

export function chooseSharePaths(): Promise<string[]> {
  return invoke<string[]>("choose_share_paths");
}

export function chooseFolderPath(): Promise<string | null> {
  return invoke<string | null>("choose_folder_path");
}

export function openPathLocation(path: string): Promise<void> {
  return invoke<void>("open_path_location", { path });
}

export function removeTransferRecord(transferId: string): Promise<void> {
  return invoke<void>("remove_transfer_record", { transferId });
}

export function clearFinishedTransfers(): Promise<void> {
  return invoke<void>("clear_finished_transfers");
}

export function listSharedResources(): Promise<ShareItem[]> {
  return invoke<ShareItem[]>("list_shared_resources");
}

export function listMyShares(): Promise<ShareItem[]> {
  return invoke<ShareItem[]>("list_my_shares");
}

export function addSharePaths(
  paths: string[],
  category: string,
  permission: string,
  password?: string,
): Promise<ShareItem[]> {
  return invoke<ShareItem[]>("add_share_paths", {
    paths,
    category,
    permission,
    password: password || null,
  });
}

export function updateShare(shareId: string, path: string): Promise<ShareItem> {
  return invoke<ShareItem>("update_share", { shareId, path });
}

export function removeShare(shareId: string): Promise<void> {
  return invoke<void>("remove_share", { shareId });
}

export function downloadShare(shareId: string, password?: string): Promise<string> {
  return invoke<string>("download_share", { shareId, password: password || null });
}

export function getLibrarySettings(): Promise<LibrarySettings> {
  return invoke<LibrarySettings>("get_library_settings");
}

export function updateLibrarySettings(settings: LibrarySettings): Promise<LibrarySettings> {
  return invoke<LibrarySettings>("update_library_settings", { settings });
}
