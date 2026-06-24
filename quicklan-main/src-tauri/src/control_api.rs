use crate::AppState;
use serde::Deserialize;
use serde_json::json;
use std::net::SocketAddr;
use tauri::Manager;
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
};

#[derive(Debug, Deserialize)]
struct SendFilesRequest {
    target_id: String,
    file_paths: Vec<String>,
}

#[derive(Debug, Deserialize)]
struct DiscoverRequest {
    ip: String,
}

pub fn start(app: tauri::AppHandle, bind: String) {
    tauri::async_runtime::spawn(async move {
        let listener = match TcpListener::bind(&bind).await {
            Ok(listener) => listener,
            Err(err) => {
                eprintln!("Codex control API failed on {bind}: {err}");
                return;
            }
        };

        loop {
            let Ok((stream, addr)) = listener.accept().await else {
                continue;
            };
            let app = app.clone();
            tauri::async_runtime::spawn(async move {
                if !addr.ip().is_loopback() {
                    return;
                }
                let _ = handle_connection(app, stream, addr).await;
            });
        }
    });
}

async fn handle_connection(
    app: tauri::AppHandle,
    mut stream: TcpStream,
    _addr: SocketAddr,
) -> Result<(), String> {
    let mut buf = vec![0_u8; 64 * 1024];
    let len = stream
        .read(&mut buf)
        .await
        .map_err(|err| format!("Failed to read control request: {err}"))?;
    if len == 0 {
        return Ok(());
    }

    let request = String::from_utf8_lossy(&buf[..len]);
    let mut lines = request.lines();
    let first = lines.next().unwrap_or_default();
    let parts: Vec<&str> = first.split_whitespace().collect();
    if parts.len() < 2 {
        return write_json(&mut stream, 400, json!({"error":"bad_request"})).await;
    }
    let method = parts[0];
    let path = parts[1];
    let body = request
        .split("\r\n\r\n")
        .nth(1)
        .or_else(|| request.split("\n\n").nth(1))
        .unwrap_or_default();

    if (method, path) == ("POST", "/show") {
        crate::show_main_window(&app);
        return write_json(&mut stream, 200, json!({"ok":true})).await;
    }

    let response = if let Some(state) = app.try_state::<AppState>() {
        match (method, path) {
            ("GET", "/health") => (
                200,
                json!({"ok":true,"app":"quick-transfer","control":"codex"}),
            ),
            ("GET", "/devices") => (200, json!(state.discovery.list_devices())),
            ("GET", "/network") => (200, json!(state.discovery.network_status())),
            ("GET", "/transfers") => (200, json!(state.transfer.list_transfers())),
            ("POST", "/discover") => {
                let req: DiscoverRequest = serde_json::from_str(body)
                    .map_err(|err| format!("Failed to parse discover request: {err}"))?;
                match state.discovery.probe_ip(req.ip) {
                    Ok(()) => (202, json!({"ok":true})),
                    Err(err) => (400, json!({"error":err})),
                }
            }
            ("POST", "/send") => {
                let req: SendFilesRequest = serde_json::from_str(body)
                    .map_err(|err| format!("Failed to parse send request: {err}"))?;
                match state.discovery.find_device(&req.target_id) {
                    Some(target) => match state.transfer.send_files(
                        target.ip,
                        target.tcp_port,
                        target.name,
                        state.discovery.local_sender(),
                        req.file_paths,
                    ) {
                        Ok(batch_id) => (202, json!({"batch_id":batch_id})),
                        Err(err) => (400, json!({"error":err})),
                    },
                    None => (404, json!({"error":"target_offline"})),
                }
            }
            _ => (404, json!({"error":"not_found"})),
        }
    } else {
        (503, json!({"error":"state_not_ready"}))
    };

    write_json(&mut stream, response.0, response.1).await
}

async fn write_json(
    stream: &mut TcpStream,
    status: u16,
    payload: serde_json::Value,
) -> Result<(), String> {
    let reason = match status {
        200 => "OK",
        202 => "Accepted",
        400 => "Bad Request",
        404 => "Not Found",
        503 => "Service Unavailable",
        _ => "OK",
    };
    let body = payload.to_string();
    let response = format!(
        "HTTP/1.1 {status} {reason}\r\nContent-Type: application/json; charset=utf-8\r\nContent-Length: {}\r\nAccess-Control-Allow-Origin: http://localhost\r\nConnection: close\r\n\r\n{body}",
        body.as_bytes().len()
    );
    stream
        .write_all(response.as_bytes())
        .await
        .map_err(|err| format!("Failed to write control response: {err}"))
}
