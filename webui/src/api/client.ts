// One authentication implementation shared with the legacy dashboard.
import "../../../lan_mesh/web/static/mesh_transport.js";

interface MeshClient {
  ensure(): Promise<void>;
  request(url: string, opts?: RequestInit): Promise<Response>;
  setUserToken(token: string): void;
  getRole(): string;
  getMeshToken(): string;
}
declare global {
  var MeshTransport: { create(): MeshClient };
}
const client = globalThis.MeshTransport.create();
export const getRole = client.getRole;
export const loginUser = client.setUserToken;
export const ensureMeshToken = client.ensure;
export const rawFetch = client.request;
export function logoutUser(): void { client.setUserToken(""); }

export async function apiFetch<T = unknown>(url: string, opts: RequestInit = {}): Promise<T> {
  const r = await rawFetch(url, opts);
  if (!r.ok) {
    let detail = r.statusText;
    try {
      const body = await r.json();
      if (body.detail) detail = String(body.detail);
    } catch { /* Keep non-JSON errors readable. */ }
    throw new Error(`${r.status} ${detail}`);
  }
  return (await r.json()) as T;
}
