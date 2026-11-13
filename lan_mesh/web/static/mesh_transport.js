/* Shared authentication and raw HTTP transport for dashboard and React adapters. */
(function (root) {
  "use strict";
  function create(options = {}) {
    const storage = options.storage || root.localStorage;
    const fetcher = options.fetch || ((...args) => root.fetch(...args));
    let meshToken = storage.getItem("lan_mesh_token") || "";
    let userToken = storage.getItem("lan_mesh_user_token") || "";
    let role = storage.getItem("lan_mesh_role") || "";
    let authPromise = null;
    let generation = 0;
    let authEnabled = false;
    function clearMeshToken() {
      meshToken = "";
      storage.removeItem("lan_mesh_token");
    }
    function state() { return { role, authEnabled }; }

    function ensure() {
      if (authPromise) return authPromise;
      const current = generation;
      let failed = false;
      const headers = new Headers();
      if (userToken || meshToken) headers.set("Authorization", `Bearer ${userToken || meshToken}`);
      authPromise = (async () => {
        try {
          const response = await fetcher("/api/station/auth-token", { headers });
          if (!response.ok) throw new Error(`Authentication bootstrap: ${response.status}`);
          const data = await response.json();
          if (current !== generation) return;
          authEnabled = Boolean(data.auth_enabled);
          role = String(data.role ?? "");
          if (!role || !authEnabled) clearMeshToken();
          if (data.auth_enabled && data.mesh_token) {
            meshToken = data.mesh_token;
            storage.setItem("lan_mesh_token", meshToken);
          }
          storage.setItem("lan_mesh_role", role);
          options.onAuthChange?.(state());
        } catch {
          failed = true;
        }
      })();
      const pending = authPromise;
      pending.then(() => {
        if (failed && current === generation && authPromise === pending) authPromise = null;
      });
      return authPromise;
    }

    async function request(url, init = {}) {
      await ensure();
      const headers = new Headers(init.headers || {});
      const token = userToken || meshToken;
      if (token) headers.set("Authorization", `Bearer ${token}`);
      if (typeof init.body === "string" && !headers.has("Content-Type")) {
        headers.set("Content-Type", "application/json");
      }
      const response = await fetcher(url, { ...init, headers });
      if (response.status === 401 || response.status === 403) {
        authPromise = null;
        await ensure();
        if (!role) options.onAuthRequired?.(state());
      }
      return response;
    }

    function setUserToken(token) {
      generation += 1;
      userToken = token.trim();
      clearMeshToken();
      role = "";
      authPromise = null;
      storage.removeItem("lan_mesh_role");
      if (userToken) storage.setItem("lan_mesh_user_token", userToken);
      else storage.removeItem("lan_mesh_user_token");
    }

    function webSocketProtocols() {
      const token = userToken || meshToken;
      return token ? ["lan-mesh", `token.${encodeURIComponent(token)}`] : ["lan-mesh"];
    }
    return { ensure, request, setUserToken, getRole: () => role,
      getMeshToken: () => meshToken, getAuthState: state, webSocketProtocols };
  }
  root.MeshTransport = Object.freeze({ create });
})(globalThis);
