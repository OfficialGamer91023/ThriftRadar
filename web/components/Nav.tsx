"use client";

import { usePathname } from "next/navigation";

const LINKS = [
  { href: "/", label: "Feed" },
  { href: "/wishlists/", label: "Wishlists" },
  { href: "/status/", label: "Status" },
];

export default function Nav() {
  const path = usePathname() || "/";
  return (
    <nav className="nav" aria-label="Main">
      {LINKS.map((l) => {
        const active = l.href === "/" ? path === "/" || path.startsWith("/listing") : path.startsWith(l.href);
        return (
          <a key={l.href} href={l.href} aria-current={active ? "page" : undefined}>
            {l.label}
          </a>
        );
      })}
    </nav>
  );
}
