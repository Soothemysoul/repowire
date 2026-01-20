'use client';

import { motion } from 'framer-motion';
import { ArrowRight, Terminal } from 'lucide-react';
import Link from 'next/link';

export default function Hero() {
  return (
    <div className="relative overflow-hidden pt-32 pb-16 sm:pb-24 lg:pb-32">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 relative z-10">
        <div className="lg:grid lg:grid-cols-12 lg:gap-8 items-center">
          <div className="sm:text-center md:max-w-2xl md:mx-auto lg:col-span-6 lg:text-left">
            <motion.div
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.5 }}
            >
              <div className="inline-flex items-center rounded-full px-3 py-1 text-sm font-semibold text-blue-600 bg-blue-50 dark:bg-blue-900/30 dark:text-blue-400 mb-6 border border-blue-100 dark:border-blue-800">
                <span>Public Beta Available</span>
              </div>
              <h1 className="text-4xl tracking-tight font-extrabold text-gray-900 dark:text-white sm:text-5xl md:text-6xl lg:text-5xl xl:text-6xl">
                <span className="block xl:inline">Mesh network for</span>{' '}
                <span className="block text-blue-600 dark:text-blue-400 xl:inline">AI coding agents</span>
              </h1>
              <p className="mt-3 text-base text-gray-500 dark:text-gray-400 sm:mt-5 sm:text-lg sm:max-w-xl sm:mx-auto md:mt-5 md:text-xl lg:mx-0">
                Stop the copy-paste dance. Enable Claude Code and OpenCode sessions to communicate directly across repositories in real-time.
              </p>
              <div className="mt-8 sm:max-w-lg sm:mx-auto sm:text-center lg:text-left lg:mx-0">
                <div className="flex flex-col sm:flex-row gap-4">
                  <Link
                    href="#installation"
                    className="inline-flex items-center justify-center px-8 py-3 border border-transparent text-base font-medium rounded-md text-white bg-blue-600 hover:bg-blue-700 md:py-4 md:text-lg transition-colors shadow-lg shadow-blue-500/20"
                  >
                    Get Started
                    <ArrowRight className="ml-2 -mr-1 w-5 h-5" />
                  </Link>
                  <Link
                    href="https://github.com/prassanna-ravishankar/repowire"
                    target="_blank"
                    className="inline-flex items-center justify-center px-8 py-3 border border-gray-300 dark:border-gray-700 text-base font-medium rounded-md text-gray-700 dark:text-gray-200 bg-white dark:bg-gray-800 hover:bg-gray-50 dark:hover:bg-gray-700 md:py-4 md:text-lg transition-colors"
                  >
                    <Terminal className="mr-2 -ml-1 w-5 h-5" />
                    View on GitHub
                  </Link>
                </div>
              </div>
            </motion.div>
          </div>
          <div className="mt-12 relative sm:max-w-lg sm:mx-auto lg:mt-0 lg:max-w-none lg:mx-0 lg:col-span-6 lg:flex lg:items-center">
            <motion.div
              initial={{ opacity: 0, scale: 0.95 }}
              animate={{ opacity: 1, scale: 1 }}
              transition={{ duration: 0.5, delay: 0.2 }}
              className="relative mx-auto w-full rounded-lg shadow-2xl lg:max-w-md bg-gray-900 overflow-hidden ring-1 ring-white/10"
            >
              <div className="flex items-center px-4 py-2 border-b border-gray-800 bg-gray-800/50">
                <div className="flex space-x-2">
                  <div className="w-3 h-3 rounded-full bg-red-500"></div>
                  <div className="w-3 h-3 rounded-full bg-yellow-500"></div>
                  <div className="w-3 h-3 rounded-full bg-green-500"></div>
                </div>
                <div className="ml-4 text-xs text-gray-400 font-mono">frontend — -zsh — 80x24</div>
              </div>
              <div className="p-6 font-mono text-sm text-gray-300 space-y-4">
                <div>
                  <span className="text-green-400">➜</span> <span className="text-blue-400">frontend</span> <span className="text-gray-500">git:(main)</span> claude
                </div>
                <div className="pl-4 border-l-2 border-gray-700">
                  <p className="text-gray-400 text-xs uppercase tracking-wider mb-1">Claude</p>
                  <p>I see you're working on the frontend. How can I help?</p>
                </div>
                <div>
                  <span className="text-green-400">➜</span> "Ask backend what API endpoints they expose"
                </div>
                <div className="pl-4 border-l-2 border-gray-700">
                  <p className="text-gray-400 text-xs uppercase tracking-wider mb-1">Repowire • Backend</p>
                  <p className="text-blue-300">@backend says:</p>
                  <p>Here are the current endpoints in `src/api.ts`:</p>
                  <ul className="list-disc list-inside text-gray-400 mt-1">
                    <li>POST /auth/login</li>
                    <li>GET /users/me</li>
                    <li>GET /projects (paginated)</li>
                  </ul>
                </div>
              </div>
            </motion.div>
          </div>
        </div>
      </div>
      
      {/* Background decoration */}
      <div className="absolute top-0 inset-x-0 h-full -z-10 overflow-hidden pointer-events-none">
        <div className="absolute left-[calc(50%-11rem)] top-[calc(50%-30rem)] h-[21.1875rem] w-[21.1875rem] -translate-x-1/2 rotate-[30deg] bg-gradient-to-tr from-[#ff80b5] to-[#9089fc] opacity-20 sm:left-[calc(50%-30rem)] sm:w-[72.1875rem]" style={{ clipPath: 'polygon(74.1% 44.1%, 100% 61.6%, 97.5% 26.9%, 85.5% 0.1%, 80.7% 2%, 72.5% 32.5%, 60.2% 62.4%, 52.4% 68.1%, 47.5% 58.3%, 45.2% 34.5%, 27.5% 76.7%, 0.1% 64.9%, 17.9% 100%, 27.6% 76.8%, 76.1% 97.7%, 74.1% 44.1%)' }}></div>
      </div>
    </div>
  );
}
