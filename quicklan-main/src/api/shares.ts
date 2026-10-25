// Tauri shares domain commands; native names and payloads are unchanged.
import { invoke } from "@tauri-apps/api/core";
import type { LibrarySettings, ShareItem } from "../types";

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
