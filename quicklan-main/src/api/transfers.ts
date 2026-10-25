// Tauri transfers domain commands; native names and payloads are unchanged.
import { invoke } from "@tauri-apps/api/core";
import type { TransferInfo } from "../types";

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

export function removeTransferRecord(transferId: string): Promise<void> {
  return invoke<void>("remove_transfer_record", { transferId });
}

export function clearFinishedTransfers(): Promise<void> {
  return invoke<void>("clear_finished_transfers");
}

