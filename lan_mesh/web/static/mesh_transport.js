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

    function ensure() {
      if (authPromise) return authPromise;
      const current = generation;
      let failed = false;
      const headers = new Headers();
      if (userToken) headers.set("Authorization", `Bearer ${userToken}`);
      authPromise = (async () => {
        try {
          const response = await fetcher("/api/station/auth-token", { headers });
          if (!response.ok) throw new Error(`Authentication bootstrap: ${response.status}`);
          const data = await response.json();
          if (current !== generation) return;
          if (data.auth_enabled && data.mesh_token) {
            meshToken = data.mesh_token;
            storage.setItem("lan_mesh_token", meshToken);
          }
          role = String(data.role ?? "");
          storage.setItem("lan_mesh_role", role);
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
      return fetcher(url, { ...init, headers });
    }

    function setUserToken(token) {
      generation += 1;
      userToken = token.trim();
      role = "";
      authPromise = null;
      storage.removeItem("lan_mesh_role");
      if (userToken) storage.setItem("lan_mesh_user_token", userToken);
      else storage.removeItem("lan_mesh_user_token");
    }

    return { ensure, request, setUserToken, getRole: () => role, getMeshToken: () => meshToken };
  }
  root.MeshTransport = Object.freeze({ create });
})(globalThis);
