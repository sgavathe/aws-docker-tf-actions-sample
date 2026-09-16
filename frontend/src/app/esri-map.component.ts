import {
  Component, ElementRef, Input, OnChanges, OnDestroy, ViewChild, SimpleChanges
} from '@angular/core';

interface LocationPoint {
  name: string;
  lat: number;
  lon: number;
}

@Component({
  selector: 'app-esri-map',
  standalone: true,
  template: `<div #mapViewNode class="map-view"></div>`,
  styles: [`
    .map-view { width: 100%; height: 400px; border-radius: 6px; overflow: hidden; }
  `]
})
export class EsriMapComponent implements OnChanges, OnDestroy {
  @Input() locations: LocationPoint[] = [];
  @ViewChild('mapViewNode', { static: true }) private mapViewEl!: ElementRef;

  private view: any;
  private graphicsLayer: any;

  async ngOnChanges(changes: SimpleChanges) {
    if (!this.view) {
      await this.initMap();
    }
    if (changes['locations'] && this.graphicsLayer) {
      this.renderPoints();
    }
  }

  private async initMap() {
    // Dynamic import keeps the ~2MB ArcGIS bundle out of the main chunk
    // until the map component actually renders.
    const [Map, MapView, GraphicsLayer] = await Promise.all([
      import('@arcgis/core/Map').then(m => m.default),
      import('@arcgis/core/views/MapView').then(m => m.default),
      import('@arcgis/core/layers/GraphicsLayer').then(m => m.default),
    ]);

    this.graphicsLayer = new GraphicsLayer();

    const map = new Map({
      // "osm" is a public basemap that does not require an ArcGIS API key.
      // Swap to "arcgis-topographic" or similar (with esriConfig.apiKey set)
      // for Esri's premium basemap styles.
      basemap: 'osm',
      layers: [this.graphicsLayer],
    });

    this.view = new MapView({
      container: this.mapViewEl.nativeElement,
      map,
      center: [-77.3, 38.0],
      zoom: 8,
    });

    this.renderPoints();
  }

  private async renderPoints() {
    if (!this.graphicsLayer) return;
    const Graphic = (await import('@arcgis/core/Graphic')).default;

    this.graphicsLayer.removeAll();
    for (const loc of this.locations) {
      this.graphicsLayer.add(new Graphic({
        geometry: { type: 'point', longitude: loc.lon, latitude: loc.lat },
        symbol: {
          type: 'simple-marker',
          color: [0, 90, 158],
          size: 10,
          outline: { color: 'white', width: 1.5 },
        },
        attributes: { name: loc.name },
        popupTemplate: { title: '{name}' },
      }));
    }
  }

  ngOnDestroy() {
    this.view?.destroy();
  }
}
