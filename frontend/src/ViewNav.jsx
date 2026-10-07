const VIEWS = [
  { id: "harbor", label: "Harbor Watch" },
  { id: "cascade", label: "Grid Cascade" },
];

/** Switch between the two demo views (hash routes, so both work from S3/nginx with no server routing). */
export default function ViewNav({ current }) {
  return (
    <nav className="views" aria-label="Demo views">
      {VIEWS.map((v) => (
        <a key={v.id} href={`#${v.id}`} aria-current={v.id === current ? "page" : undefined}>
          {v.label}
        </a>
      ))}
    </nav>
  );
}

export function viewFromHash() {
  return window.location.hash === "#cascade" ? "cascade" : "harbor";
}
