// Sector colors are categorical and hold up on both the navy panel and the light-grey
// basemap. Status is shown by form (filled = failed, ring = degraded) plus a red or
// yellow outline, so it never depends on color alone.

export const SECTORS = {
  energy: { label: "Energy", color: "#F08C2E" },
  water: { label: "Water", color: "#3A8FD9" },
  communications: { label: "Communications", color: "#9B7BE0" },
  it: { label: "Data centers", color: "#2EB3A7" },
  health: { label: "Healthcare", color: "#E0619A" },
  emergency: { label: "Emergency services", color: "#6FBF5A" },
};

export const SECTOR_ORDER = Object.keys(SECTORS);

export const STATUS = {
  Failed: { label: "Failed", color: "#FF5A5F" },
  Degraded: { label: "On backup", color: "#F2C744" },
};

// The service a dependency link carries takes the color of the sector that supplies it.
export const LINK_TYPES = {
  power: { label: "Power", color: SECTORS.energy.color },
  water: { label: "Water", color: SECTORS.water.color },
  comms: { label: "Comms", color: SECTORS.communications.color },
};

export const sectorColor = (s) => SECTORS[s]?.color ?? "#8a97a3";
export const sectorLabel = (s) => SECTORS[s]?.label ?? s;

const KIND_LABELS = {
  plant: "Power plant",
  substation: "Substation",
  water_treatment: "Water treatment plant",
  wastewater_plant: "Wastewater plant",
  pumping_station: "Water pumping station",
  sewage_pumping: "Sewage lift station",
  water_tower: "Water tower",
  telecom_exchange: "Telephone exchange",
  comm_tower: "Communications tower",
  data_center: "Data center",
  hospital: "Hospital",
  fire_station: "Fire station",
  police: "Police station",
  ambulance_station: "EMS station",
};

export const kindLabel = (k) => KIND_LABELS[k] ?? k.replaceAll("_", " ");

/** [r, g, b, a] for the ArcGIS symbol API. */
export function rgba(hex, alpha = 1) {
  const n = parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255, alpha];
}
