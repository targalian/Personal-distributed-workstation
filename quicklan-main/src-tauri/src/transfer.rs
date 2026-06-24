use crate::{
    lan_api,
    library::{DownloadSource, LibraryService},
    protocol::{
        DiscoveryPacket,
        FileHeader, IncomingTransferEvent, SenderInfo, SharedDownloadRequest,
        SharedDownloadResponse, TcpHeader, TransferCompletedEvent, TransferDirection,
        TransferFailedEvent, TransferInfo, TransferStatus, CHUNK_SIZE, DISCOVERY_PORT, TCP_PORT,
    },
    settings::SettingsService,
    storage,
};
use sha2::{Digest, Sha256};
use std::{
    collections::HashMap,
    net::{IpAddr, Ipv4Addr, SocketAddr, TcpListener as StdTcpListener, UdpSocket},
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::Instant,
};
use tauri::{AppHandle, Emitter, Manager, WebviewUrl, WebviewWindowBuilder};
use tokio::{
    fs::File,
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
    sync::oneshot,
};
use uuid::Uuid;

#[derive(Clone)]
pub struct TransferService {
    app: AppHandle,
    settings: SettingsService,
    library: LibraryService,
    port: Arc<Mutex<u16>>,
    api_port: u16,
    transfers: Arc<Mutex<HashMap<String, TransferInfo>>>,
    decisions: Arc<Mutex<HashMap<String, oneshot::Sender<bool>>>>,
}

fn bind_transfer_listener(start_port: u16) -> Result<(u16, StdTcpListener), String> {
    let mut last_error = None;
    for port in start_port..start_port + 20 {
        let bind = format!("0.0.0.0:{port}");
        match StdTcpListener::bind(&bind) {
            Ok(listener) => {
                listener
                    .set_nonblocking(true)
                    .map_err(|err| format!("配置 TCP 监听失败 {bind}: {err}"))?;
                return Ok((port, listener));
            }
            Err(err) => last_error = Some(format!("{bind}: {err}")),
        }
    }
    Err(format!(
        "无法启动 TCP 文件接收监听，端口 {}-{} 都不可用{}",
        start_port,
        start_port + 19,
        last_error
            .map(|err| format!("；最后错误：{err}"))
            .unwrap_or_default()
    ))
}

fn destination_folder_path(destination: &Path) -> String {
    destination
        .parent()
        .unwrap_or(destination)
        .display()
        .to_string()
}

impl TransferService {
    pub fn new(app: AppHandle, settings: SettingsService, library: LibraryService, api_port: u16) -> Self {
        Self {
            app,
            settings,
            library,
            port: Arc::new(Mutex::new(TCP_PORT)),
            api_port,
            transfers: Arc::new(Mutex::new(HashMap::new())),
            decisions: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    pub fn port(&self) -> u16 {
        self.port.lock().map(|port| *port).unwrap_or(TCP_PORT)
    }

    pub fn list_transfers(&self) -> Vec<TransferInfo> {
        let mut transfers: Vec<TransferInfo> = self
            .transfers
            .lock()
            .map(|map| map.values().cloned().collect())
            .unwrap_or_default();
        transfers.sort_by(|a, b| b.id.cmp(&a.id));
        transfers
    }

    pub fn get_transfer(&self, transfer_id: &str) -> Option<TransferInfo> {
        self.transfers
            .lock()
            .ok()
            .and_then(|map| map.get(transfer_id).cloned())
    }

    pub fn remove_transfer(&self, transfer_id: &str) -> Result<(), String> {
        self.transfers
            .lock()
            .map_err(|_| "传输记录正在被占用".to_string())?
            .remove(transfer_id);
        Ok(())
    }

    pub fn clear_finished(&self) -> Result<(), String> {
        self.transfers
            .lock()
            .map_err(|_| "传输记录正在被占用".to_string())?
            .retain(|_, transfer| {
                matches!(
                    transfer.status,
                    TransferStatus::Pending
                        | TransferStatus::WaitingForReceiver
                        | TransferStatus::Transferring
                )
            });
        Ok(())
    }

    pub fn start_listener(&self) -> Result<u16, String> {
        let (port, listener) = bind_transfer_listener(TCP_PORT)?;
        if let Ok(mut current_port) = self.port.lock() {
            *current_port = port;
        }

        let service = self.clone();
        tauri::async_runtime::spawn(async move {
            let listener = match TcpListener::from_std(listener) {
                Ok(listener) => listener,
                Err(err) => {
                    eprintln!("TCP listener failed on 0.0.0.0:{port}: {err}");
                    return;
                }
            };

            loop {
                let Ok((stream, peer)) = listener.accept().await else {
                    continue;
                };
                let service = service.clone();
                tauri::async_runtime::spawn(async move {
                    if let Err(err) = service.handle_incoming(stream, peer.ip().to_string()).await {
                        eprintln!("incoming TCP failed: {err}");
                    }
                });
            }
        });
        Ok(port)
    }

    pub fn accept(&self, transfer_id: &str) -> Result<(), String> {
        self.decide(transfer_id, true)
    }

    pub fn reject(&self, transfer_id: &str) -> Result<(), String> {
        self.decide(transfer_id, false)
    }

    fn decide(&self, transfer_id: &str, accepted: bool) -> Result<(), String> {
        let tx = self
            .decisions
            .lock()
            .map_err(|_| "确认队列正在被占用".to_string())?
            .remove(transfer_id)
            .ok_or_else(|| "传输请求不存在或已处理".to_string())?;
        tx.send(accepted)
            .map_err(|_| "接收任务已结束，无法处理确认".to_string())
    }

    pub fn send_files(
        &self,
        target_ip: String,
        target_port: u16,
        target_name: String,
        sender: SenderInfo,
        file_paths: Vec<String>,
    ) -> Result<String, String> {
        if file_paths.is_empty() {
            return Err("请选择至少一个文件".to_string());
        }

        let batch_id = Uuid::new_v4().to_string();
        for path in file_paths {
            let service = self.clone();
            let batch_id = batch_id.clone();
            let target_ip = target_ip.clone();
            let target_name = target_name.clone();
            let sender = sender.clone();
            tauri::async_runtime::spawn(async move {
                let error_batch_id = batch_id.clone();
                let error_peer_name = target_name.clone();
                let error_peer_ip = target_ip.clone();
                if let Err(err) = service
                    .send_one_file(
                        PathBuf::from(path),
                        target_ip,
                        target_port,
                        target_name,
                        sender,
                        batch_id,
                    )
                    .await
                {
                    service.emit_failure(error_batch_id, error_peer_name, error_peer_ip, err);
                }
            });
        }

        Ok(batch_id)
    }

    pub fn download_shared(
        &self,
        source: DownloadSource,
        requester: SenderInfo,
        password: Option<String>,
    ) -> Result<String, String> {
        let transfer_id = Uuid::new_v4().to_string();
        let batch_id = Uuid::new_v4().to_string();
        let transfer = TransferInfo {
            id: transfer_id.clone(),
            batch_id: batch_id.clone(),
            file_name: source.name.clone(),
            file_size: source.size,
            bytes_done: 0,
            speed_bps: 0.0,
            eta_secs: None,
            direction: TransferDirection::Receiving,
            status: TransferStatus::Pending,
            peer_name: source.device_name.clone(),
            peer_ip: source.ip.clone(),
            message: Some("等待下载".to_string()),
            save_path: None,
            share_id: Some(source.share_id.clone()),
            version: Some(source.version),
            file_hash: Some(source.file_hash.clone()),
        };
        self.upsert_and_emit("transfer-progress", transfer.clone());

        let service = self.clone();
        tauri::async_runtime::spawn(async move {
            if source.is_local {
                service.complete_local_download(transfer, source).await;
                return;
            }
            if let Err(err) = service.download_from_source(transfer, source, requester, password).await {
                service.emit_failure(batch_id, "共享下载".to_string(), String::new(), err);
            }
        });
        Ok(transfer_id)
    }

    async fn complete_local_download(&self, mut transfer: TransferInfo, source: DownloadSource) {
        let store_path = storage::shared_content_path(&source.file_hash);
        let destination = match storage::ensure_download_dir(self.settings.download_dir()).await {
            Ok(dir) => match storage::unique_destination(&dir, &source.name).await {
                Ok(path) => path,
                Err(err) => {
                    transfer.status = TransferStatus::Failed;
                    transfer.message = Some(err);
                    self.upsert_and_emit("transfer-failed", TransferFailedEvent { transfer });
                    return;
                }
            },
            Err(err) => {
                transfer.status = TransferStatus::Failed;
                transfer.message = Some(err);
                self.upsert_and_emit("transfer-failed", TransferFailedEvent { transfer });
                return;
            }
        };
        if let Err(err) = tokio::fs::copy(&store_path, &destination).await {
            transfer.status = TransferStatus::Failed;
            transfer.message = Some(format!("从共享副本保存到下载目录失败: {err}"));
            self.upsert_and_emit("transfer-failed", TransferFailedEvent { transfer });
            return;
        }
        transfer.bytes_done = transfer.file_size;
        transfer.status = TransferStatus::Completed;
        transfer.message = Some("已从本机共享副本保存到下载目录".to_string());
        transfer.save_path = Some(destination_folder_path(&destination));
        self.upsert_and_emit(
            "transfer-completed",
            TransferCompletedEvent { transfer },
        );
    }

    async fn send_one_file(
        &self,
        path: PathBuf,
        target_ip: String,
        target_port: u16,
        target_name: String,
        sender: SenderInfo,
        batch_id: String,
    ) -> Result<(), String> {
        let metadata = tokio::fs::metadata(&path)
            .await
            .map_err(|err| format!("读取文件失败 {}: {err}", path.display()))?;
        if !metadata.is_file() {
            return Err(format!("只能发送文件: {}", path.display()));
        }

        let file_name = storage::path_file_name(&path)?;
        let file_size = metadata.len();
        let transfer_id = Uuid::new_v4().to_string();
        let transfer = TransferInfo {
            id: transfer_id.clone(),
            batch_id: batch_id.clone(),
            file_name: file_name.clone(),
            file_size,
            bytes_done: 0,
            speed_bps: 0.0,
            eta_secs: None,
            direction: TransferDirection::Sending,
            status: TransferStatus::WaitingForReceiver,
            peer_name: target_name.clone(),
            peer_ip: target_ip.clone(),
            message: Some("正在计算 SHA256".to_string()),
            save_path: None,
            share_id: None,
            version: None,
            file_hash: None,
        };
        self.upsert_and_emit("transfer-progress", transfer.clone());

        let sha256 = sha256_file(&path, file_size, |bytes_done, speed_bps, eta_secs| {
            let mut progress = transfer.clone();
            progress.bytes_done = bytes_done;
            progress.speed_bps = speed_bps;
            progress.eta_secs = eta_secs;
            progress.message = Some("正在计算 SHA256".to_string());
            self.upsert_and_emit("transfer-progress", progress);
        })
        .await?;

        let mut stream = TcpStream::connect(format!("{target_ip}:{target_port}"))
            .await
            .map_err(|err| format!("连接 {target_name} 失败，可能被 Windows 防火墙拦截: {err}"))?;
        let header = TcpHeader::QuickSend(FileHeader {
            transfer_id: transfer_id.clone(),
            batch_id,
            file_name: file_name.clone(),
            file_size,
            sha256,
            sender,
        });
        write_json_frame(&mut stream, &header).await?;

        let mut response = [0_u8; 1];
        stream
            .read_exact(&mut response)
            .await
            .map_err(|err| format!("等待接收端确认失败: {err}"))?;
        if response[0] != 1 {
            let mut rejected = transfer.clone();
            rejected.status = TransferStatus::Rejected;
            rejected.message = Some("接收端已拒绝".to_string());
            self.upsert_and_emit("transfer-failed", TransferFailedEvent { transfer: rejected });
            return Ok(());
        }

        self.stream_file_to_peer(&mut stream, &path, transfer, "正在发送".to_string())
            .await
    }

    async fn handle_incoming(&self, mut stream: TcpStream, peer_ip: String) -> Result<(), String> {
        match read_json_frame::<TcpHeader>(&mut stream).await? {
            TcpHeader::QuickSend(header) => self.handle_quick_send(stream, peer_ip, header).await,
            TcpHeader::SharedDownload(request) => {
                self.handle_shared_download(stream, peer_ip, request).await
            }
        }
    }

    async fn handle_quick_send(
        &self,
        mut stream: TcpStream,
        peer_ip: String,
        header: FileHeader,
    ) -> Result<(), String> {
        let transfer = TransferInfo {
            id: header.transfer_id.clone(),
            batch_id: header.batch_id.clone(),
            file_name: header.file_name.clone(),
            file_size: header.file_size,
            bytes_done: 0,
            speed_bps: 0.0,
            eta_secs: None,
            direction: TransferDirection::Receiving,
            status: TransferStatus::Pending,
            peer_name: header.sender.device_name.clone(),
            peer_ip: peer_ip.clone(),
            message: Some("等待确认".to_string()),
            save_path: None,
            share_id: None,
            version: None,
            file_hash: None,
        };

        let (tx, rx) = oneshot::channel();
        self.decisions
            .lock()
            .map_err(|_| "确认队列正在被占用".to_string())?
            .insert(header.transfer_id.clone(), tx);
        self.upsert_and_emit(
            "incoming-transfer",
            IncomingTransferEvent {
                transfer: transfer.clone(),
            },
        );
        self.upsert_and_emit("transfer-progress", transfer.clone());
        self.open_incoming_window(&transfer);

        let accepted = rx.await.unwrap_or(false);
        if !accepted {
            let _ = stream.write_all(&[0]).await;
            let mut rejected = transfer;
            rejected.status = TransferStatus::Rejected;
            rejected.message = Some("已拒绝".to_string());
            self.upsert_and_emit("transfer-failed", TransferFailedEvent { transfer: rejected });
            return Ok(());
        }
        stream
            .write_all(&[1])
            .await
            .map_err(|err| format!("发送确认失败: {err}"))?;

        let dir = storage::ensure_download_dir(self.settings.download_dir()).await?;
        let destination = storage::unique_destination(&dir, &header.file_name).await?;
        self.receive_file(stream, header.sha256, transfer, destination, None)
            .await
    }

    async fn handle_shared_download(
        &self,
        mut stream: TcpStream,
        peer_ip: String,
        request: SharedDownloadRequest,
    ) -> Result<(), String> {
        let content = match self
            .library
            .shared_content(
                &request.share_id,
                request.version,
                &request.file_hash,
                request.password.as_deref(),
            )
        {
            Ok(content) => content,
            Err(err) => {
                let response = SharedDownloadResponse {
                    ok: false,
                    message: Some(err),
                    name: None,
                    size: None,
                    file_hash: None,
                };
                write_json_frame(&mut stream, &response).await?;
                return Ok(());
            }
        };
        let response = SharedDownloadResponse {
            ok: true,
            message: None,
            name: Some(content.name.clone()),
            size: Some(content.size),
            file_hash: Some(content.file_hash.clone()),
        };
        write_json_frame(&mut stream, &response).await?;

        let transfer = TransferInfo {
            id: request.transfer_id,
            batch_id: Uuid::new_v4().to_string(),
            file_name: content.name,
            file_size: content.size,
            bytes_done: 0,
            speed_bps: 0.0,
            eta_secs: None,
            direction: TransferDirection::Sending,
            status: TransferStatus::Transferring,
            peer_name: request.requester.device_name,
            peer_ip,
            message: Some("正在提供共享副本".to_string()),
            save_path: Some(content.path.display().to_string()),
            share_id: Some(content.share_id),
            version: Some(content.version),
            file_hash: Some(content.file_hash),
        };
        self.stream_file_to_peer(&mut stream, &content.path, transfer, "正在提供共享副本".to_string())
            .await
    }

    async fn download_from_source(
        &self,
        base: TransferInfo,
        source: DownloadSource,
        requester: SenderInfo,
        password: Option<String>,
    ) -> Result<(), String> {
        let mut stream = TcpStream::connect(format!("{}:{}", source.ip, source.tcp_port))
            .await
            .map_err(|err| format!("连接下载源失败: {err}"))?;
        let request = TcpHeader::SharedDownload(SharedDownloadRequest {
            transfer_id: base.id.clone(),
            share_id: source.share_id.clone(),
            version: source.version,
            file_hash: source.file_hash.clone(),
            requester,
            password,
        });
        write_json_frame(&mut stream, &request).await?;
        let response = read_json_frame::<SharedDownloadResponse>(&mut stream).await?;
        if !response.ok {
            return Err(response.message.unwrap_or_else(|| "下载源拒绝请求".to_string()));
        }
        let dir = storage::ensure_download_dir(self.settings.download_dir()).await?;
        let destination = storage::unique_destination(&dir, &source.name).await?;
        self.receive_file(
            stream,
            source.file_hash.clone(),
            base,
            destination.clone(),
            Some(source.clone()),
        )
        .await?;
        lan_api::post_download_completed(&source.ip, source.api_port, &source.share_id);
        Ok(())
    }

    async fn receive_file(
        &self,
        mut stream: TcpStream,
        expected_hash: String,
        base: TransferInfo,
        destination: PathBuf,
        source: Option<DownloadSource>,
    ) -> Result<(), String> {
        let mut output = File::create(&destination)
            .await
            .map_err(|err| format!("创建接收文件失败 {}: {err}", destination.display()))?;
        let mut hasher = Sha256::new();
        let mut buf = vec![0_u8; CHUNK_SIZE];
        let mut bytes_done = 0_u64;
        let total = base.file_size;
        let start = Instant::now();

        while bytes_done < total {
            let remaining = (total - bytes_done) as usize;
            let read_size = remaining.min(buf.len());
            let read = stream
                .read(&mut buf[..read_size])
                .await
                .map_err(|err| format!("接收文件失败: {err}"))?;
            if read == 0 {
                return self.fail_receive(base, "连接已中断".to_string()).await;
            }
            output
                .write_all(&buf[..read])
                .await
                .map_err(|err| format!("写入文件失败: {err}"))?;
            hasher.update(&buf[..read]);
            bytes_done += read as u64;

            let (speed_bps, eta_secs) = progress_stats(start, bytes_done, total);
            let mut progress = base.clone();
            progress.bytes_done = bytes_done;
            progress.speed_bps = speed_bps;
            progress.eta_secs = eta_secs;
            progress.status = TransferStatus::Transferring;
            progress.message = Some("正在接收".to_string());
            progress.save_path = Some(destination_folder_path(&destination));
            self.upsert_and_emit("transfer-progress", progress);
        }

        output
            .flush()
            .await
            .map_err(|err| format!("刷新文件失败: {err}"))?;
        let actual = format!("{:x}", hasher.finalize());
        if actual != expected_hash {
            return self
                .fail_receive(base, "SHA256 校验失败，文件可能已损坏".to_string())
                .await;
        }

        if let Some(source) = source {
            self.library.register_local_replica(
                &source.share_id,
                source.version,
                &source.file_hash,
                &source.name,
                source.size,
                &destination,
            )?;
            self.broadcast_library_update();
        }

        let mut completed = base;
        completed.bytes_done = total;
        completed.status = TransferStatus::Completed;
        completed.message = Some("接收完成，SHA256 校验通过".to_string());
        completed.save_path = Some(destination_folder_path(&destination));
        self.upsert_and_emit("transfer-completed", TransferCompletedEvent { transfer: completed });
        Ok(())
    }

    async fn stream_file_to_peer(
        &self,
        stream: &mut TcpStream,
        path: &Path,
        base: TransferInfo,
        message: String,
    ) -> Result<(), String> {
        let mut file = File::open(path)
            .await
            .map_err(|err| format!("打开文件失败 {}: {err}", path.display()))?;
        let mut buf = vec![0_u8; CHUNK_SIZE];
        let start = Instant::now();
        let mut bytes_done = 0_u64;

        loop {
            let read = file
                .read(&mut buf)
                .await
                .map_err(|err| format!("读取文件失败: {err}"))?;
            if read == 0 {
                break;
            }
            stream
                .write_all(&buf[..read])
                .await
                .map_err(|err| format!("发送文件失败: {err}"))?;
            bytes_done += read as u64;
            let (speed_bps, eta_secs) = progress_stats(start, bytes_done, base.file_size);
            let mut progress = base.clone();
            progress.bytes_done = bytes_done;
            progress.speed_bps = speed_bps;
            progress.eta_secs = eta_secs;
            progress.status = TransferStatus::Transferring;
            progress.message = Some(message.clone());
            self.upsert_and_emit("transfer-progress", progress);
        }

        let mut completed = base;
        completed.bytes_done = completed.file_size;
        completed.status = TransferStatus::Completed;
        completed.message = Some("发送完成".to_string());
        self.upsert_and_emit("transfer-completed", TransferCompletedEvent { transfer: completed });
        Ok(())
    }

    async fn fail_receive(&self, mut transfer: TransferInfo, message: String) -> Result<(), String> {
        transfer.status = TransferStatus::Failed;
        transfer.message = Some(message);
        self.upsert_and_emit("transfer-failed", TransferFailedEvent { transfer });
        Ok(())
    }

    fn emit_failure(&self, batch_id: String, peer_name: String, peer_ip: String, message: String) {
        let transfer = TransferInfo {
            id: Uuid::new_v4().to_string(),
            batch_id,
            file_name: "传输任务".to_string(),
            file_size: 0,
            bytes_done: 0,
            speed_bps: 0.0,
            eta_secs: None,
            direction: TransferDirection::Sending,
            status: TransferStatus::Failed,
            peer_name,
            peer_ip,
            message: Some(message),
            save_path: None,
            share_id: None,
            version: None,
            file_hash: None,
        };
        self.upsert_and_emit("transfer-failed", TransferFailedEvent { transfer });
    }

    fn broadcast_library_update(&self) {
        let summary = self.library.summary();
        let packet = DiscoveryPacket {
            app: "quicklan".to_string(),
            version: 1,
            packet_type: "library".to_string(),
            device_id: self.library.device_id(),
            device_name: self.library.device_name(),
            tcp_port: self.port(),
            api_port: self.api_port,
            library_version: summary.library_version,
            share_count: summary.share_count,
            manifest_hash: summary.manifest_hash,
            upload_tasks: 0,
        };
        if let Ok(socket) = UdpSocket::bind((Ipv4Addr::UNSPECIFIED, 0)) {
            let _ = socket.set_broadcast(true);
            if let Ok(payload) = serde_json::to_vec(&packet) {
                let _ = socket.send_to(
                    &payload,
                    SocketAddr::new(IpAddr::V4(Ipv4Addr::BROADCAST), DISCOVERY_PORT),
                );
            }
        }
    }

    fn open_incoming_window(&self, transfer: &TransferInfo) {
        let label = format!("incoming-{}", transfer.id);
        if self.app.get_webview_window(&label).is_some() {
            return;
        }
        let url = format!("index.html?mode=incoming&transfer_id={}", transfer.id);
        let _ = WebviewWindowBuilder::new(&self.app, label, WebviewUrl::App(url.into()))
            .title("接收文件")
            .inner_size(420.0, 260.0)
            .resizable(false)
            .always_on_top(true)
            .build();
    }

    fn upsert_and_emit<T>(&self, event: &str, payload: T)
    where
        T: serde::Serialize + Clone,
    {
        if let Ok(value) = serde_json::to_value(&payload) {
            let maybe_transfer = value
                .get("transfer")
                .cloned()
                .or_else(|| Some(value.clone()))
                .and_then(|value| serde_json::from_value::<TransferInfo>(value).ok());
            if let Some(transfer) = maybe_transfer {
                if let Ok(mut map) = self.transfers.lock() {
                    map.insert(transfer.id.clone(), transfer);
                }
            }
        }
        let _ = self.app.emit(event, payload);
    }
}

async fn write_json_frame<T: serde::Serialize>(stream: &mut TcpStream, value: &T) -> Result<(), String> {
    let payload = serde_json::to_vec(value).map_err(|err| format!("序列化传输头失败: {err}"))?;
    let len: u32 = payload
        .len()
        .try_into()
        .map_err(|_| "传输头过大".to_string())?;
    stream
        .write_all(&len.to_be_bytes())
        .await
        .map_err(|err| format!("发送传输头长度失败: {err}"))?;
    stream
        .write_all(&payload)
        .await
        .map_err(|err| format!("发送传输头失败: {err}"))
}

async fn read_json_frame<T: for<'de> serde::Deserialize<'de>>(stream: &mut TcpStream) -> Result<T, String> {
    let mut len_buf = [0_u8; 4];
    stream
        .read_exact(&mut len_buf)
        .await
        .map_err(|err| format!("读取传输头长度失败: {err}"))?;
    let len = u32::from_be_bytes(len_buf) as usize;
    if len == 0 || len > 128 * 1024 {
        return Err("传输头长度非法".to_string());
    }
    let mut payload = vec![0_u8; len];
    stream
        .read_exact(&mut payload)
        .await
        .map_err(|err| format!("读取传输头失败: {err}"))?;
    serde_json::from_slice(&payload).map_err(|err| format!("解析传输头失败: {err}"))
}

async fn sha256_file<F>(path: &Path, file_size: u64, mut on_progress: F) -> Result<String, String>
where
    F: FnMut(u64, f64, Option<u64>),
{
    let mut file = File::open(path)
        .await
        .map_err(|err| format!("打开文件失败 {}: {err}", path.display()))?;
    let mut hasher = Sha256::new();
    let mut buf = vec![0_u8; CHUNK_SIZE];
    let mut bytes_done = 0_u64;
    let start = Instant::now();

    loop {
        let read = file
            .read(&mut buf)
            .await
            .map_err(|err| format!("计算 SHA256 失败: {err}"))?;
        if read == 0 {
            break;
        }
        hasher.update(&buf[..read]);
        bytes_done += read as u64;
        let (speed_bps, eta_secs) = progress_stats(start, bytes_done, file_size);
        on_progress(bytes_done, speed_bps, eta_secs);
    }

    Ok(format!("{:x}", hasher.finalize()))
}

fn progress_stats(start: Instant, bytes_done: u64, total: u64) -> (f64, Option<u64>) {
    let elapsed = start.elapsed().as_secs_f64().max(0.001);
    let speed_bps = bytes_done as f64 / elapsed;
    let eta_secs = if speed_bps > 0.0 && bytes_done < total {
        Some(((total - bytes_done) as f64 / speed_bps).ceil() as u64)
    } else {
        None
    };
    (speed_bps, eta_secs)
}
