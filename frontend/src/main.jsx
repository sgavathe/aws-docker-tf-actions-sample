import React from "react";
import ReactDOM from "react-dom/client";
import esriConfig from "@arcgis/core/config";
import "@arcgis/core/assets/esri/themes/light/main.css";
import "./App.css";
import App from "./App.jsx";

// By default @arcgis/core pulls workers/translations from the js.arcgis.com CDN.
// On locked-down (e.g., government) networks, self-host them instead:
//   cp -R node_modules/@arcgis/core/assets public/esri-assets
//   VITE_ARCGIS_ASSETS=/esri-assets npm run build
if (import.meta.env.VITE_ARCGIS_ASSETS) {
  esriConfig.assetsPath = import.meta.env.VITE_ARCGIS_ASSETS;
}

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
