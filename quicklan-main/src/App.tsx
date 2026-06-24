import { listen } from "@tauri-apps/api/event";
import { getCurrentWebview } from "@tauri-apps/api/webview";
import { getCurrentWindow } from "@tauri-apps/api/window";
import {
  Check,
  Clock,
  Download,
  FilePlus,
  FolderOpen,
  HardDrive,
  Info,
  Laptop,
  Library,
  Network,
  RefreshCw,
  Save,
  Search,
  Send,
  Settings,
  Share2,
  ShieldCheck,
  StickyNote,
  Trash2,
  UploadCloud,
  X,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import {
  acceptTransfer,
  addSharePaths,
  chooseDownloadDir,
  chooseFolderPath,
  chooseSharePaths,
  discoverIp,
  downloadShare,
  getAppInfo,
  getControlApiInfo,
  getLibrarySettings,
  getNetworkStatus,
  getSettings,
  getTransfer,
  getTransfers,
  listDevices,
  listMyShares,
  listSharedResources,
  openPathLocation,
  clearFinishedTransfers,
  rejectTransfer,
  removeTransferRecord,
  removeShare,
  sendFiles,
  updateDeviceNote,
  updateLibrarySettings,
  updateNickname,
  updateShare,
} from "./api";
import type {
  AppSettings,
  AppInfo,
  ControlApiInfo,
  DeviceInfo,
  IncomingTransferPayload,
  LibrarySettings,
  NetworkStatus,
  ShareItem,
  TransferInfo,
  TransferPayload,
} from "./types";
import "./styles.css";

type Tab = "devices" | "store" | "mine" | "settings";

function unwrapTransfer(payload: TransferPayload): TransferInfo {
  if ("transfer" in payload) return payload.transfer;
  return payload;
}

export default function App() {
  const params = new URLSearchParams(window.location.search);
  if (params.get("mode") === "incoming") {
    return <IncomingWindow transferId={params.get("transfer_id") ?? ""} />;
  }
  return <MainWindow />;
}

function MainWindow() {
  const [tab, setTab] = useState<Tab>("devices");
  const [devices, setDevices] = useState<DeviceInfo[]>([]);
  const [shares, setShares] = useState<ShareItem[]>([]);
  const [myShares, setMyShares] = useState<ShareItem[]>([]);
  const [transfers, setTransfers] = useState<TransferInfo[]>([]);
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [appInfo, setAppInfo] = useState<AppInfo | null>(null);
  const [librarySettings, setLibrarySettings] = useState<LibrarySettings | null>(null);
  const [controlApi, setControlApi] = useState<ControlApiInfo | null>(null);
  const [networkStatus, setNetworkStatus] = useState<NetworkStatus | null>(null);
  const [selectedDeviceId, setSelectedDeviceId] = useState<string | null>(null);
  const [filePaths, setFilePaths] = useState<string[]>([]);
  const [manualIp, setManualIp] = useState("");
  const [nicknameDraft, setNicknameDraft] = useState("");
  const [search, setSearch] = useState("");
  const [category, setCategory] = useState("全部");
  const [sort, setSort] = useState("updated");
  const [shareCategory, setShareCategory] = useState("文档");
  const [sharePermission, setSharePermission] = useState("public");
  const [sharePassword, setSharePassword] = useState("");
  const [downloadPassword, setDownloadPassword] = useState("");
  const [pendingPasswordShare, setPendingPasswordShare] = useState<ShareItem | null>(null);
  const [noteDevice, setNoteDevice] = useState<DeviceInfo | null>(null);
  const [noteDraft, setNoteDraft] = useState("");
  const [transfersOpen, setTransfersOpen] = useState(true);
  const [shareDraftPaths, setShareDraftPaths] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const selectedDevice = devices.find((device) => device.id === selectedDeviceId);
  const onlineCount = devices.filter((device) => device.online).length;

  const filteredShares = useMemo(() => {
    const q = search.trim().toLowerCase();
    const result = shares.filter((share) => {
      const matchesCategory = category === "全部" || share.category === category;
      const matchesSearch =
        !q ||
        share.name.toLowerCase().includes(q) ||
        share.owner_name.toLowerCase().includes(q) ||
        share.file_hash.toLowerCase().includes(q);
      return matchesCategory && matchesSearch;
    });
    return [...result].sort((a, b) => {
      if (sort === "size") return b.size - a.size;
      if (sort === "downloads") return b.download_count - a.download_count;
      if (sort === "name") return a.name.localeCompare(b.name);
      return b.updated_at - a.updated_at;
    });
  }, [shares, search, category, sort]);

  const categories = useMemo(
    () => ["全部", ...Array.from(new Set(shares.map((share) => share.category))).sort()],
    [shares],
  );

  useEffect(() => {
    void refreshAll();
    const unsubscribers = [
      listen<DeviceInfo[]>("devices-updated", (event) => setDevices(event.payload)),
      listen<ShareItem[]>("library-updated", (event) => {
        setShares(event.payload);
        void refreshShares();
      }),
      listen<IncomingTransferPayload>("incoming-transfer", (event) => {
        upsertTransfer(event.payload.transfer);
      }),
      listen<TransferPayload>("transfer-progress", (event) => {
        upsertTransfer(unwrapTransfer(event.payload));
      }),
      listen<TransferPayload>("transfer-completed", (event) => {
        upsertTransfer(unwrapTransfer(event.payload));
        void refreshShares();
      }),
      listen<TransferPayload>("transfer-failed", (event) => {
        upsertTransfer(unwrapTransfer(event.payload));
      }),
      getCurrentWebview().onDragDropEvent((event) => {
        const payload = event.payload;
        if (payload.type !== "drop" || payload.paths.length === 0) return;
        if (tab === "mine") addShareDraftPaths(payload.paths);
        else addQuickSendFiles(payload.paths);
      }),
    ];

    return () => {
      void Promise.all(unsubscribers).then((items) => {
        items.forEach((unsubscribe) => unsubscribe());
      });
    };
  }, [tab]);

  async function refreshAll() {
    const [deviceList, transferList, status, appSettings, nextLibrarySettings, api, info] =
      await Promise.all([
        listDevices(),
        getTransfers(),
        getNetworkStatus(),
        getSettings(),
        getLibrarySettings(),
        getControlApiInfo(),
        getAppInfo(),
      ]);
    setDevices(deviceList);
    setTransfers(transferList);
    setNetworkStatus(status);
    setSettings(appSettings);
    setNicknameDraft(appSettings.nickname);
    setLibrarySettings(nextLibrarySettings);
    setControlApi(api);
    setAppInfo(info);
    await refreshShares();
  }

  async function refreshShares() {
    const [nextShares, nextMyShares] = await Promise.all([listSharedResources(), listMyShares()]);
    setShares(nextShares);
    setMyShares(nextMyShares);
  }

  function upsertTransfer(next: TransferInfo) {
    setTransfers((current) => {
      const rest = current.filter((transfer) => transfer.id !== next.id);
      return [next, ...rest].slice(0, 100);
    });
  }

  function showToast(message: string) {
    setToast(message);
    window.setTimeout(() => setToast(null), 2400);
  }

  function addQuickSendFiles(paths: string[]) {
    const onlyFiles = paths.filter(Boolean);
    if (onlyFiles.length === 0) return;
    setFilePaths((current) => Array.from(new Set([...current, ...onlyFiles])));
    setError(null);
  }

  function addShareDraftPaths(paths: string[]) {
    const next = paths.filter(Boolean);
    if (next.length === 0) return;
    setShareDraftPaths((current) => Array.from(new Set([...current, ...next])));
    setError(null);
  }

  async function runAction(action: () => Promise<void>, success?: string) {
    try {
      setBusy(true);
      setError(null);
      await action();
      if (success) showToast(success);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function startDownload(share: ShareItem, password?: string) {
    await downloadShare(share.share_id, password);
    setTransfersOpen(true);
  }

  return (
    <main className="shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">QuickLAN</p>
          <h1>局域网共享与快传</h1>
        </div>
        <div className="status-strip">
          <span>
            <Network size={16} /> {onlineCount} 台在线
          </span>
          <span>
            <Library size={16} /> {shares.length} 个资源
          </span>
          <span>
            <ShieldCheck size={16} /> 去中心化索引
          </span>
        </div>
      </header>

      <nav className="tabs">
        <TabButton active={tab === "devices"} icon={<Laptop size={17} />} onClick={() => setTab("devices")}>
          设备
        </TabButton>
        <TabButton active={tab === "store"} icon={<Library size={17} />} onClick={() => setTab("store")}>
          共享广场
        </TabButton>
        <TabButton active={tab === "mine"} icon={<Share2 size={17} />} onClick={() => setTab("mine")}>
          我的共享
        </TabButton>
        <TabButton active={tab === "settings"} icon={<Settings size={17} />} onClick={() => setTab("settings")}>
          设置
        </TabButton>
        <button
          className="icon-button"
          disabled={busy}
          onClick={() => void runAction(refreshAll, "刷新成功")}
          title="刷新"
        >
          <RefreshCw size={17} />
        </button>
      </nav>

      {error && (
        <div className="error">
          <span>{error}</span>
          <button className="icon-button" onClick={() => setError(null)} title="关闭">
            <X size={15} />
          </button>
        </div>
      )}

      {tab === "devices" && (
        <DevicesTab
          devices={devices}
          selectedDeviceId={selectedDeviceId}
          selectedDevice={selectedDevice}
          filePaths={filePaths}
          manualIp={manualIp}
          busy={busy}
          onSelectDevice={setSelectedDeviceId}
          onManualIp={setManualIp}
          onAddFiles={addQuickSendFiles}
          onChooseFiles={() =>
            void runAction(async () => {
              addQuickSendFiles(await chooseSharePaths());
            })
          }
          onChooseFolder={() =>
            void runAction(async () => {
              const folder = await chooseFolderPath();
              if (folder) addQuickSendFiles([folder]);
            })
          }
          onProbe={() =>
            void runAction(async () => {
              if (!manualIp.trim()) throw new Error("请输入对方局域网 IP 地址");
              await discoverIp(manualIp.trim());
              setTimeout(() => void refreshAll(), 500);
            }, "已发送探测广播")
          }
          onEditNote={(device) => {
            setNoteDevice(device);
            setNoteDraft(device.note ?? "");
          }}
          onSend={() =>
            void runAction(async () => {
              if (!selectedDeviceId) throw new Error("请先选择目标设备");
              if (filePaths.length === 0) throw new Error("请先添加要快传的文件");
              await sendFiles(selectedDeviceId, filePaths);
              setFilePaths([]);
            }, "快传任务已创建")
          }
          onRemoveFile={(path) => setFilePaths((items) => items.filter((item) => item !== path))}
        />
      )}

      {tab === "store" && (
        <StoreTab
          shares={filteredShares}
          categories={categories}
          search={search}
          category={category}
          sort={sort}
          busy={busy}
          onSearch={setSearch}
          onCategory={setCategory}
          onSort={setSort}
          onDownload={(share) =>
            void runAction(async () => {
              if (share.permission === "password") {
                setPendingPasswordShare(share);
                setDownloadPassword("");
                return;
              }
              await startDownload(share);
            })
          }
        />
      )}

      {tab === "mine" && (
        <MineTab
          shares={myShares}
          draftPaths={shareDraftPaths}
          category={shareCategory}
          permission={sharePermission}
          password={sharePassword}
          busy={busy}
          onCategory={setShareCategory}
          onPermission={setSharePermission}
          onPassword={setSharePassword}
          onAddDraftPaths={addShareDraftPaths}
          onChoosePaths={() =>
            void runAction(async () => {
              addShareDraftPaths(await chooseSharePaths());
            })
          }
          onChooseFolder={() =>
            void runAction(async () => {
              const folder = await chooseFolderPath();
              if (folder) addShareDraftPaths([folder]);
            })
          }
          onRemoveDraftPath={(path) =>
            setShareDraftPaths((items) => items.filter((item) => item !== path))
          }
          onPublish={() =>
            void runAction(async () => {
              if (shareDraftPaths.length === 0) throw new Error("请先选择要共享的文件");
              await addSharePaths(shareDraftPaths, shareCategory, sharePermission, sharePassword);
              setShareDraftPaths([]);
              await refreshShares();
            }, "共享索引已发布")
          }
          onUpdate={(shareId) =>
            void runAction(async () => {
              const paths = await chooseSharePaths();
              if (paths.length === 0) return;
              await updateShare(shareId, paths[0]);
              await refreshShares();
            }, "共享版本已更新")
          }
          onRemove={(shareId) =>
            void runAction(async () => {
              await removeShare(shareId);
              await refreshShares();
            }, "共享已取消")
          }
        />
      )}

      {tab === "settings" && settings && librarySettings && (
        <SettingsTab
          settings={settings}
          appInfo={appInfo}
          librarySettings={librarySettings}
          nicknameDraft={nicknameDraft}
          networkStatus={networkStatus}
          controlApi={controlApi}
          busy={busy}
          onNicknameDraft={setNicknameDraft}
          onSaveNickname={() =>
            void runAction(async () => {
              const next = await updateNickname(nicknameDraft);
              setSettings(next);
              setNicknameDraft(next.nickname);
            }, "昵称已保存")
          }
          onChooseDownloadDir={() =>
            void runAction(async () => {
              const next = await chooseDownloadDir();
              if (next) setSettings(next);
            })
          }
          onOpenDownloadDir={() =>
            void runAction(async () => {
              await openPathLocation(settings.download_dir);
            })
          }
          onLibrarySettings={(next) =>
            void runAction(async () => {
              setLibrarySettings(await updateLibrarySettings(next));
            }, "共享设置已保存")
          }
        />
      )}

      <TransfersPanel
        transfers={transfers}
        open={transfersOpen}
        onToggle={() => setTransfersOpen((value) => !value)}
        onRemove={(transferId) =>
          void runAction(async () => {
            await removeTransferRecord(transferId);
            setTransfers(await getTransfers());
          })
        }
        onClearFinished={() =>
          void runAction(async () => {
            await clearFinishedTransfers();
            setTransfers(await getTransfers());
          }, "已清理已结束传输")
        }
      />
      {pendingPasswordShare && (
        <div className="modal-backdrop">
          <div className="modal">
            <h2>输入访问密码</h2>
            <p className="muted">{pendingPasswordShare.name}</p>
            <input
              type="password"
              value={downloadPassword}
              onChange={(event) => setDownloadPassword(event.target.value)}
              placeholder="共享密码"
              autoFocus
            />
            <div className="modal-actions">
              <button
                onClick={() => {
                  setPendingPasswordShare(null);
                  setDownloadPassword("");
                }}
              >
                取消
              </button>
              <button
                className="primary"
                onClick={() =>
                  void runAction(async () => {
                    await startDownload(pendingPasswordShare, downloadPassword);
                    setPendingPasswordShare(null);
                    setDownloadPassword("");
                  }, "下载任务已创建")
                }
              >
                <Download size={16} /> 下载
              </button>
            </div>
          </div>
        </div>
      )}
      {noteDevice && (
        <div className="modal-backdrop">
          <div className="modal">
            <h2>编辑备注</h2>
            <p className="muted">{noteDevice.name}</p>
            <input
              value={noteDraft}
              onChange={(event) => setNoteDraft(event.target.value)}
              placeholder="给这台设备添加本地备注"
              autoFocus
              maxLength={48}
            />
            <div className="modal-actions">
              <button
                onClick={() => {
                  setNoteDevice(null);
                  setNoteDraft("");
                }}
              >
                取消
              </button>
              <button
                className="primary"
                onClick={() =>
                  void runAction(async () => {
                    setDevices(await updateDeviceNote(noteDevice.id, noteDraft));
                    setNoteDevice(null);
                    setNoteDraft("");
                  }, "备注已保存")
                }
              >
                <Save size={16} /> 保存
              </button>
            </div>
          </div>
        </div>
      )}
      {toast && <div className="toast">{toast}</div>}
    </main>
  );
}

function TabButton({
  active,
  icon,
  children,
  onClick,
}: {
  active: boolean;
  icon: React.ReactNode;
  children: React.ReactNode;
  onClick: () => void;
}) {
  return (
    <button className={`tab ${active ? "active" : ""}`} onClick={onClick}>
      {icon}
      {children}
    </button>
  );
}

function DevicesTab(props: {
  devices: DeviceInfo[];
  selectedDeviceId: string | null;
  selectedDevice?: DeviceInfo;
  filePaths: string[];
  manualIp: string;
  busy: boolean;
  onSelectDevice: (id: string) => void;
  onManualIp: (value: string) => void;
  onAddFiles: (paths: string[]) => void;
  onChooseFiles: () => void;
  onChooseFolder: () => void;
  onProbe: () => void;
  onEditNote: (device: DeviceInfo) => void;
  onSend: () => void;
  onRemoveFile: (path: string) => void;
}) {
  return (
    <section className="grid two">
      <section className="panel">
        <div className="panel-title">
          <h2>在线设备</h2>
          <span className="pill">{props.devices.length} 台</span>
        </div>
        <div className="manual-probe">
          <input
            value={props.manualIp}
            onChange={(event) => props.onManualIp(event.target.value)}
            placeholder="手动探测 IP，如 192.168.1.23"
          />
          <button className="icon-button" onClick={props.onProbe} title="探测 IP">
            <Search size={16} />
          </button>
        </div>
        <div className="device-list">
          {props.devices.length === 0 ? (
            <Empty icon={<Laptop size={34} />} text="等待局域网内的 QuickLAN 设备出现" />
          ) : (
            props.devices.map((device) => (
              <div
                key={device.id}
                className={`device ${props.selectedDeviceId === device.id ? "selected" : ""}`}
              >
                <button className="device-main" onClick={() => props.onSelectDevice(device.id)}>
                  <span className={`dot ${device.online ? "online" : ""}`} />
                  <span>
                    <strong>{device.note ? `${device.note}（${device.name}）` : device.name}</strong>
                    <small>
                      {device.ip}:{device.tcp_port} · {device.share_count} 个资源
                    </small>
                  </span>
                </button>
                <button className="icon-button" onClick={() => props.onEditNote(device)} title="编辑备注">
                  <StickyNote size={15} />
                </button>
              </div>
            ))
          )}
        </div>
      </section>

      <section className="panel stack">
        <div className="panel-title">
          <h2>文件快传</h2>
          <button className="secondary" onClick={props.onChooseFiles}>
            <FilePlus size={16} /> 选择文件
          </button>
          <button className="secondary" onClick={props.onChooseFolder}>
            <FolderOpen size={16} /> 文件夹
          </button>
        </div>
        <Dropzone
          icon={<UploadCloud size={42} />}
          title="拖入文件进行点对点快传"
          text="接收方确认后开始传输，完成后进行 SHA256 校验。"
          onDrop={props.onAddFiles}
        />
        <div className="sendbar">
          <div>
            <strong>{props.selectedDevice ? props.selectedDevice.name : "未选择设备"}</strong>
            <span>{props.filePaths.length > 0 ? `已选择 ${props.filePaths.length} 个文件` : "添加文件并选择设备后即可发送"}</span>
          </div>
          <button className="primary" disabled={props.busy} onClick={props.onSend}>
            <Send size={17} /> 发送
          </button>
        </div>
        <PathList paths={props.filePaths} onRemove={props.onRemoveFile} />
      </section>
    </section>
  );
}

function StoreTab(props: {
  shares: ShareItem[];
  categories: string[];
  search: string;
  category: string;
  sort: string;
  busy: boolean;
  onSearch: (value: string) => void;
  onCategory: (value: string) => void;
  onSort: (value: string) => void;
  onDownload: (share: ShareItem) => void;
}) {
  return (
    <section className="panel stack">
      <div className="toolbar">
        <div className="searchbox">
          <Search size={16} />
          <input value={props.search} onChange={(event) => props.onSearch(event.target.value)} placeholder="搜索文件、分享者或 hash" />
        </div>
        <select value={props.category} onChange={(event) => props.onCategory(event.target.value)}>
          {props.categories.map((item) => (
            <option key={item}>{item}</option>
          ))}
        </select>
        <select value={props.sort} onChange={(event) => props.onSort(event.target.value)}>
          <option value="updated">最近更新</option>
          <option value="downloads">下载次数</option>
          <option value="size">文件大小</option>
          <option value="name">文件名</option>
        </select>
      </div>
      <div className="resource-list">
        {props.shares.length === 0 ? (
          <Empty icon={<Library size={34} />} text="暂无可下载的共享资源" />
        ) : (
          props.shares.map((share) => (
            <ShareRow
              key={share.share_id}
              share={share}
              action={
                <button className="primary compact" disabled={props.busy} onClick={() => props.onDownload(share)}>
                  <Download size={16} /> 下载
                </button>
              }
            />
          ))
        )}
      </div>
    </section>
  );
}

function MineTab(props: {
  shares: ShareItem[];
  draftPaths: string[];
  category: string;
  permission: string;
  password: string;
  busy: boolean;
  onCategory: (value: string) => void;
  onPermission: (value: string) => void;
  onPassword: (value: string) => void;
  onAddDraftPaths: (paths: string[]) => void;
  onChoosePaths: () => void;
  onChooseFolder: () => void;
  onRemoveDraftPath: (path: string) => void;
  onPublish: () => void;
  onUpdate: (shareId: string) => void;
  onRemove: (shareId: string) => void;
}) {
  return (
    <section className="grid two">
      <section className="panel stack">
        <div className="panel-title">
          <h2>发布共享索引</h2>
          <button className="secondary" onClick={props.onChoosePaths}>
            <FilePlus size={16} /> 选择文件
          </button>
          <button className="secondary" onClick={props.onChooseFolder}>
            <FolderOpen size={16} /> 文件夹
          </button>
        </div>
        <Dropzone
          icon={<HardDrive size={42} />}
          title="拖入文件加入 Shared Store"
          text="发布时会创建共享副本，原始文件后续修改不会影响已共享版本。"
          onDrop={props.onAddDraftPaths}
        />
        <div className="form-grid">
          <label>
            分类
            <select value={props.category} onChange={(event) => props.onCategory(event.target.value)}>
              <option>文档</option>
              <option>图片</option>
              <option>视频</option>
              <option>软件</option>
              <option>其他</option>
            </select>
          </label>
          <label>
            权限
            <select value={props.permission} onChange={(event) => props.onPermission(event.target.value)}>
              <option value="public">公开共享</option>
              <option value="password">密码共享</option>
            </select>
          </label>
          {props.permission === "password" && (
            <label>
              密码
              <input value={props.password} onChange={(event) => props.onPassword(event.target.value)} placeholder="访问密码" />
            </label>
          )}
        </div>
        <PathList paths={props.draftPaths} onRemove={props.onRemoveDraftPath} />
        <button className="primary" disabled={props.busy} onClick={props.onPublish}>
          <Share2 size={17} /> 发布共享
        </button>
      </section>

      <section className="panel stack">
        <div className="panel-title">
          <h2>我的共享</h2>
          <span className="pill">{props.shares.length} 个</span>
        </div>
        <div className="resource-list">
          {props.shares.length === 0 ? (
            <Empty icon={<Share2 size={34} />} text="还没有发布共享资源" />
          ) : (
            props.shares.map((share) => (
              <ShareRow
                key={share.share_id}
                share={share}
                action={
                  <div className="row-actions">
                    <button className="secondary compact" onClick={() => props.onUpdate(share.share_id)}>
                      <RefreshCw size={15} /> 更新
                    </button>
                    <button className="icon-button danger" onClick={() => props.onRemove(share.share_id)} title="取消共享">
                      <Trash2 size={15} />
                    </button>
                  </div>
                }
              />
            ))
          )}
        </div>
      </section>
    </section>
  );
}

function SettingsTab(props: {
  settings: AppSettings;
  appInfo: AppInfo | null;
  librarySettings: LibrarySettings;
  nicknameDraft: string;
  networkStatus: NetworkStatus | null;
  controlApi: ControlApiInfo | null;
  busy: boolean;
  onNicknameDraft: (value: string) => void;
  onSaveNickname: () => void;
  onChooseDownloadDir: () => void;
  onOpenDownloadDir: () => void;
  onLibrarySettings: (settings: LibrarySettings) => void;
}) {
  const update = (patch: Partial<LibrarySettings>) =>
    props.onLibrarySettings({ ...props.librarySettings, ...patch });

  return (
    <section className="grid two">
      <section className="panel stack">
        <h2>本机设置</h2>
        <div className="settings-row">
          <Info size={17} />
          <strong>当前版本</strong>
          <code>{props.appInfo?.version ?? "0.1.0"}</code>
        </div>
        <div className="settings-row">
          <Settings size={17} />
          <strong>昵称</strong>
          <input value={props.nicknameDraft} onChange={(event) => props.onNicknameDraft(event.target.value)} />
          <button className="secondary" disabled={props.busy} onClick={props.onSaveNickname}>
            <Save size={16} /> 保存
          </button>
        </div>
        <div className="settings-row">
          <FolderOpen size={17} />
          <strong>下载目录</strong>
          <button className="path-link" onClick={props.onOpenDownloadDir} title="打开下载目录">
            {props.settings.download_dir}
          </button>
          <button className="secondary" onClick={props.onChooseDownloadDir}>
            浏览
          </button>
        </div>
        <div className="diagnostics">
          <strong>网络</strong>
          <span>UDP {props.networkStatus?.udp_port ?? "--"}</span>
          <span>TCP {props.networkStatus?.tcp_port ?? "--"}</span>
          <span>HTTP {props.networkStatus?.api_port ?? "--"}</span>
          <span>{props.networkStatus?.local_ips.join(", ") || "无本机局域网 IP"}</span>
        </div>
        <footer className="control-api">
          Codex 本地控制 API <code>{props.controlApi?.enabled ? `http://${props.controlApi.bind}` : "未启用"}</code>
        </footer>
      </section>

      <section className="panel stack">
        <h2>资源加速与缓存</h2>
        <label className="toggle-row">
          <input
            type="checkbox"
            checked={props.librarySettings.acceleration_enabled}
            onChange={(event) => update({ acceleration_enabled: event.target.checked })}
          />
          参与资源加速
        </label>
        <div className="form-grid">
          <label>
            最大上传速度
            <select value={props.librarySettings.max_upload_speed} onChange={(event) => update({ max_upload_speed: event.target.value })}>
              <option value="unlimited">不限</option>
              <option value="10MB/s">10MB/s</option>
              <option value="50MB/s">50MB/s</option>
              <option value="100MB/s">100MB/s</option>
            </select>
          </label>
          <label>
            最大上传任务
            <select value={props.librarySettings.max_upload_tasks} onChange={(event) => update({ max_upload_tasks: Number(event.target.value) })}>
              {[1, 2, 3, 5].map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </label>
          <label>
            缓存上限
            <select value={props.librarySettings.cache_limit_gb} onChange={(event) => update({ cache_limit_gb: Number(event.target.value) })}>
              {[10, 50, 100, 500].map((value) => (
                <option key={value} value={value}>
                  {value}GB
                </option>
              ))}
            </select>
          </label>
        </div>
      </section>
    </section>
  );
}

function Dropzone(props: {
  icon: React.ReactNode;
  title: string;
  text: string;
  onDrop: (paths: string[]) => void;
}) {
  return (
    <div
      className="dropzone"
      onDragOver={(event) => event.preventDefault()}
      onDrop={(event) => {
        event.preventDefault();
      }}
    >
      {props.icon}
      <h2>{props.title}</h2>
      <p>{props.text}</p>
    </div>
  );
}

function PathList({ paths, onRemove }: { paths: string[]; onRemove: (path: string) => void }) {
  if (paths.length === 0) return null;
  return (
    <div className="file-list">
      {paths.map((path) => (
        <div className="file-row" key={path}>
          <span title={path}>{basename(path)}</span>
          <button className="icon-button" title="移除" onClick={() => onRemove(path)}>
            <X size={15} />
          </button>
        </div>
      ))}
    </div>
  );
}

function ShareRow({ share, action }: { share: ShareItem; action: React.ReactNode }) {
  return (
    <article className="resource">
      <div className="resource-main">
        <strong>{share.name}</strong>
        <span>
          {share.owner_name} · v{share.latest_version} · {formatBytes(share.size)}
        </span>
        <small title={share.file_hash}>
          上传/更新 {formatDateTime(share.updated_at)} · {share.file_hash.slice(0, 16)} · {share.replica_count} 个副本节点
        </small>
      </div>
      <div className="resource-meta">
        <span className="pill">{share.category}</span>
        <span className="pill">{share.permission === "password" ? "密码" : "公开"}</span>
        <span>{share.download_count} 次下载</span>
      </div>
      {action}
    </article>
  );
}

function TransfersPanel({
  transfers,
  open,
  onToggle,
  onRemove,
  onClearFinished,
}: {
  transfers: TransferInfo[];
  open: boolean;
  onToggle: () => void;
  onRemove: (transferId: string) => void;
  onClearFinished: () => void;
}) {
  const activeCount = transfers.filter((transfer) =>
    ["pending", "waiting_for_receiver", "transferring"].includes(transfer.status),
  ).length;
  return (
    <section className="panel stack">
      <div className="panel-title">
        <button className="section-toggle" onClick={onToggle}>
          <Clock size={17} />
          <h2>传输任务</h2>
          <span className="pill">{activeCount} 进行中 / {transfers.length} 总计</span>
        </button>
        <button className="secondary compact" onClick={onClearFinished}>
          <Trash2 size={15} /> 清理已结束
        </button>
      </div>
      {!open ? null : transfers.length === 0 ? (
        <Empty icon={<Clock size={30} />} text="暂无传输任务" />
      ) : (
        <div className="transfer-grid">
          {transfers.slice(0, 8).map((transfer) => (
            <TransferRow key={transfer.id} transfer={transfer} onRemove={onRemove} />
          ))}
        </div>
      )}
    </section>
  );
}

function TransferRow({
  transfer,
  onRemove,
}: {
  transfer: TransferInfo;
  onRemove: (transferId: string) => void;
}) {
  const percent =
    transfer.file_size > 0 ? Math.min(100, (transfer.bytes_done / transfer.file_size) * 100) : 0;
  const canOpen = transfer.direction === "receiving" && transfer.save_path;
  return (
    <article className={`transfer ${transfer.status}`}>
      <div className="transfer-head">
        <div>
          <strong>{transfer.file_name}</strong>
          <span>
            {transfer.direction === "sending" ? "发送到" : "来自"} {transfer.peer_name}
          </span>
        </div>
        <div className="row-actions">
          <StatusBadge status={transfer.status} />
          <button className="icon-button" title="删除记录" onClick={() => onRemove(transfer.id)}>
            <Trash2 size={15} />
          </button>
        </div>
      </div>
      <div className="progress">
        <span style={{ width: `${percent}%` }} />
      </div>
      <div className="transfer-meta">
        <span>{formatBytes(transfer.bytes_done)} / {formatBytes(transfer.file_size)}</span>
        <span>{formatSpeed(transfer.speed_bps)}</span>
        <span>{transfer.eta_secs == null ? "ETA --" : `剩余 ${formatDuration(transfer.eta_secs)}`}</span>
      </div>
      {transfer.message && <p className="message">{transfer.message}</p>}
      {canOpen && (
        <button className="secondary fit" onClick={() => void openPathLocation(transfer.save_path!)}>
          <FolderOpen size={16} /> 打开位置
        </button>
      )}
    </article>
  );
}

function StatusBadge({ status }: { status: TransferInfo["status"] }) {
  const labels: Record<TransferInfo["status"], string> = {
    pending: "待处理",
    waiting_for_receiver: "等待接收",
    transferring: "传输中",
    completed: "完成",
    rejected: "已拒绝",
    failed: "失败",
  };
  return <span className={`badge ${status}`}>{labels[status]}</span>;
}

function IncomingWindow({ transferId }: { transferId: string }) {
  const [transfer, setTransfer] = useState<TransferInfo | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void getTransfer(transferId).then(setTransfer);
  }, [transferId]);

  async function answer(accepted: boolean) {
    try {
      if (accepted) {
        await acceptTransfer(transferId);
      } else {
        await rejectTransfer(transferId);
      }
      await getCurrentWindow().close();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <main className="incoming-window">
      <h1>接收文件？</h1>
      {transfer ? (
        <>
          <p>
            <strong>{transfer.peer_name}</strong> 想发送文件给你
          </p>
          <div className="incoming-file">
            <strong>{transfer.file_name}</strong>
            <span>{formatBytes(transfer.file_size)}</span>
          </div>
          {error && <div className="error">{error}</div>}
          <div className="modal-actions">
            <button onClick={() => void answer(false)}>拒绝</button>
            <button className="primary" onClick={() => void answer(true)}>
              <Check size={17} /> 接收
            </button>
          </div>
        </>
      ) : (
        <p>正在读取传输请求...</p>
      )}
    </main>
  );
}

function Empty({ icon, text }: { icon: React.ReactNode; text: string }) {
  return (
    <div className="empty">
      {icon}
      <p>{text}</p>
    </div>
  );
}

function basename(path: string) {
  return path.split(/[\\/]/).pop() ?? path;
}

function formatDateTime(seconds: number) {
  if (!seconds) return "--";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(seconds * 1000));
}

function formatBytes(value: number) {
  if (value < 1024) return `${value} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let size = value / 1024;
  let unit = units[0];
  for (let i = 1; i < units.length && size >= 1024; i += 1) {
    size /= 1024;
    unit = units[i];
  }
  return `${size.toFixed(size >= 10 ? 1 : 2)} ${unit}`;
}

function formatSpeed(value: number) {
  if (!Number.isFinite(value) || value <= 0) return "--/s";
  return `${formatBytes(value)}/s`;
}

function formatDuration(seconds: number) {
  if (seconds < 60) return `${seconds}s`;
  const mins = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return `${mins}m ${rest}s`;
}
