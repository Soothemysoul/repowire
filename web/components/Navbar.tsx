import Link from 'next/link';
import Image from 'next/image';
import { Github } from 'lucide-react';

export default function Navbar() {
  return (
    <nav className="fixed top-0 w-full z-50 bg-white/80 dark:bg-black/80 backdrop-blur-md border-b border-gray-200 dark:border-gray-800">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
        <div className="flex justify-between h-16 items-center">
          <div className="flex-shrink-0 flex items-center gap-2">
            <Link href="/" className="flex items-center gap-2">
              <div className="relative w-8 h-8">
                <Image
                  src="/logo-light.webp"
                  alt="Repowire Logo"
                  fill
                  sizes="32px"
                  className="object-contain dark:hidden"
                />
                <Image
                  src="/logo-dark.webp"
                  alt="Repowire Logo"
                  fill
                  sizes="32px"
                  className="object-contain hidden dark:block"
                />
              </div>
              <span className="font-bold text-xl tracking-tight">Repowire</span>
            </Link>
          </div>
          <div className="flex items-center gap-4">
            <Link 
              href="https://github.com/prassanna-ravishankar/repowire"
              target="_blank"
              rel="noopener noreferrer"
              className="text-gray-500 hover:text-gray-900 dark:text-gray-400 dark:hover:text-white transition-colors"
            >
              <Github className="w-5 h-5" />
            </Link>
          </div>
        </div>
      </div>
    </nav>
  );
}
