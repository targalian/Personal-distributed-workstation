export type DeviceInfo = {
  id: string;
  name: string;
  ip: string;
  tcp_port: number;
  api_port: number;
  online: boolean;
  last_seen_ms: number;
  share_count: number;
  library_version: number;
  manifest_hash: string;
  upload_tasks: number;
  latency_ms: number | null;
  note: string | null;
};

export type TransferStatus =
  | "pending"
  | "waiting_for_receiver"
  | "transferring"
  | "completed"
  | "rejected"
  | "failed";

export type TransferDirection = "sending" | "receiving";

export type TransferInfo = {
  id: string;
  batch_id: string;
  file_name: string;
  file_size: number;
  bytes_done: number;
  speed_bps: number;
  eta_secs: number | null;
  direction: TransferDirection;
  status: TransferStatus;
  peer_name: string;
  peer_ip: string;
  message: string | null;
  save_path: string | null;
  share_id: string | null;
  version: number | null;
  file_hash: string | null;
};

export type ControlApiInfo = {
  enabled: boolean;
  bind: string;
};

export type AppInfo = {
  version: string;
};

export type AppSettings = {
  nickname: string;
  download_dir: string;
};

export type LibrarySettings = {
  acceleration_enabled: boolean;
  max_upload_speed: string;
  max_upload_tasks: number;
  cache_limit_gb: number;
};

export type NetworkStatus = {
  udp_port: number;
  tcp_port: number;
  api_port: number;
  local_ips: string[];
  broadcast_targets: string[];
};

export type ShareItem = {
  share_id: string;
  name: string;
  category: string;
  permission: string;
  owner_device_id: string;
  owner_name: string;
  latest_version: number;
  file_hash: string;
  size: number;
  created_at: number;
  updated_at: number;
  download_count: number;
  replica_count: number;
  is_local: boolean;
  active: boolean;
};

export type ShareVersion = {
  share_id: string;
  version: number;
  file_hash: string;
  size: number;
  created_at: number;
};

export type ReplicaNode = {
  share_id: string;
  version: number;
  file_hash: string;
  device_id: string;
  device_name: string;
  ip: string;
  tcp_port: number;
  api_port: number;
  online: boolean;
  upload_tasks: number;
  latency_ms: number | null;
  is_local: boolean;
};

export type Manifest = {
  device_id: string;
  device_name: string;
  library_version: number;
  manifest_hash: string;
};

export type IncomingTransferPayload = {
  transfer: TransferInfo;
};

export type TransferPayload = TransferInfo | { transfer: TransferInfo };
