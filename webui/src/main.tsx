import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./styles.css";

// iter-56: SPA 入口 — 由服务端 /spa 路由托管构建产物
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
