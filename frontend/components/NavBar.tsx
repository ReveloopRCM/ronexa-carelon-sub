"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { getMe, logout } from "@/lib/api";

export default function NavBar() {
  const [username, setUsername] = useState<string | null>(null);
  const pathname = usePathname();
  const router = useRouter();

  useEffect(() => {
    if (pathname === "/login") return;
    getMe().then((user) => {
      if (user) setUsername(user.username);
    });
  }, [pathname]);

  const handleLogout = async () => {
    await logout();
    router.push("/login");
  };

  // Don't show nav on login page
  if (pathname === "/login") return null;

  const navLinks = [
    { href: "/", label: "Dashboard" },
    { href: "/queues", label: "Queues" },
    { href: "/worklist", label: "Worklist" },
    { href: "/cases", label: "Cases" },
    { href: "/queue", label: "Review" },
    { href: "/awaiting-clinicals", label: "Awaiting Clinicals" },
    { href: "/availity", label: "Availity" },
    { href: "/executions", label: "Executions" },
    { href: "/analytics", label: "Analytics" },
  ];

  const secondaryLinks = [
    { href: "/upload", label: "Upload" },
    { href: "/settings", label: "Settings" },
  ];

  return (
    <nav className="border-b bg-white px-6 py-3 flex items-center gap-6">
      <Link href="/" className="font-bold text-lg">
        Ronexa
      </Link>
      <div className="h-4 w-px bg-gray-300" />
      {navLinks.map((link) => (
        <Link
          key={link.href}
          href={link.href}
          className={`text-sm ${
            pathname === link.href
              ? "text-blue-600 font-medium"
              : "text-gray-600 hover:text-gray-900"
          }`}
        >
          {link.label}
        </Link>
      ))}
      <div className="h-4 w-px bg-gray-300" />
      {secondaryLinks.map((link) => (
        <Link
          key={link.href}
          href={link.href}
          className={`text-sm ${
            pathname === link.href
              ? "text-blue-600 font-medium"
              : "text-gray-600 hover:text-gray-900"
          }`}
        >
          {link.label}
        </Link>
      ))}

      {/* User section — pushed to right */}
      <div className="ml-auto flex items-center gap-3">
        {username && (
          <>
            <span className="text-sm text-gray-500">{username}</span>
            <button
              onClick={handleLogout}
              className="text-sm text-gray-500 hover:text-red-600 transition-colors"
            >
              Logout
            </button>
          </>
        )}
      </div>
    </nav>
  );
}
