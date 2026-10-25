// Tauri devices domain commands; native names and payloads are unchanged.
import { invoke } from "@tauri-apps/api/core";
import type { AppInfo, ControlApiInfo, DeviceInfo, NetworkStatus } from "../types";

export function listDevices(): Promise<DeviceInfo[]> {
  return invoke<DeviceInfo[]>("list_devices");
}

export function updateDeviceNote(deviceId: string, note: string): Promise<DeviceInfo[]> {
  return invoke<DeviceInfo[]>("update_device_note", { deviceId, note });
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

