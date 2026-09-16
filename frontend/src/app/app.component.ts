import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { HttpClient, HttpClientModule } from '@angular/common/http';
import { EsriMapComponent } from './esri-map.component';

interface Location {
  name: string;
  lat: number;
  lon: number;
}

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [CommonModule, HttpClientModule, EsriMapComponent],
  template: `
    <div class="container">
      <h1>Geo Demo App</h1>
      <p>Backend: {{ apiBase }}</p>

      <app-esri-map [locations]="locations"></app-esri-map>

      <ul>
        <li *ngFor="let loc of locations">
          {{ loc.name }} — ({{ loc.lat }}, {{ loc.lon }})
        </li>
      </ul>
      <p *ngIf="error" class="error">{{ error }}</p>
    </div>
  `,
  styles: [`
    .container { font-family: sans-serif; max-width: 700px; margin: 40px auto; }
    .error { color: red; }
    li { margin: 6px 0; }
  `]
})
export class AppComponent implements OnInit {
  locations: Location[] = [];
  error = '';
  apiBase = (window as any).__env?.apiBase || 'http://localhost:8080';

  constructor(private http: HttpClient) {}

  ngOnInit() {
    this.http.get<Location[]>(`${this.apiBase}/api/locations`).subscribe({
      next: (data) => (this.locations = data),
      error: () => (this.error = 'Could not reach backend API'),
    });
  }
}
