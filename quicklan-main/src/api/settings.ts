// Tauri settings domain commands; native names and payloads are unchanged.
import { invoke } from "@tauri-apps/api/core";
import type { AppSettings } from "../types";

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

