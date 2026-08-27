"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const LINKS = [
  { href: "/", label: "Control Tower" },
  { href: "/incidents", label: "Incidents" },
  { href: "/experiments", label: "Experiment" },
  { href: "/audit", label: "Audit Explorer" },
];

export default function Nav() {
  const pathname = usePathname();
  return (
    <nav className="nav">
      <span className="brand">RECOVERYOS</span>
      {LINKS.map((link) => (
        <Link
          key={link.href}
          href={link.href}
          className={pathname === link.href ? "active" : ""}
        >
          {link.label}
        </Link>
      ))}
    </nav>
  );
}
