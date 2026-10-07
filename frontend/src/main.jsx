import React, { useEffect, useState } from "react";
import ReactDOM from "react-dom/client";
import esriConfig from "@arcgis/core/config";
import "@arcgis/core/assets/esri/themes/light/main.css";
import "./App.css";
import App from "./App.jsx";
import CascadeApp from "./cascade/CascadeApp.jsx";
import { viewFromHash } from "./ViewNav.jsx";

// By default @arcgis/core pulls workers/translations from the js.arcgis.com CDN.
// On locked-down (e.g., government) networks, self-host them instead:
//   cp -R node_modules/@arcgis/core/assets public/esri-assets
//   VITE_ARCGIS_ASSETS=/esri-assets npm run build
if (import.meta.env.VITE_ARCGIS_ASSETS) {
  esriConfig.assetsPath = import.meta.env.VITE_ARCGIS_ASSETS;
}

function Root() {
  const [view, setView] = useState(viewFromHash);
  useEffect(() => {
    const onHash = () => setView(viewFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  return view === "cascade" ? <CascadeApp /> : <App />;
}

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <Root />
  </React.StrictMode>
);
