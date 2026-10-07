import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { initThemeSync } from "./theme";
import "./index.css";

initThemeSync();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
