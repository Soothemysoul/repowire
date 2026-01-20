export default function HowItWorks() {
  return (
    <div className="py-24 bg-white dark:bg-black overflow-hidden lg:py-32">
      <div className="relative max-w-xl mx-auto px-4 sm:px-6 lg:px-8 lg:max-w-7xl">
        <div className="relative">
          <h2 className="text-center text-3xl leading-8 font-extrabold tracking-tight text-gray-900 dark:text-white sm:text-4xl">
            How it works
          </h2>
          <p className="mt-4 max-w-3xl mx-auto text-center text-xl text-gray-500 dark:text-gray-400">
            Repowire sits between your Claude sessions, acting as a message broker.
          </p>
        </div>

        <div className="relative mt-12 lg:mt-24 lg:grid lg:grid-cols-2 lg:gap-8 lg:items-center">
          <div className="relative">
            <h3 className="text-2xl font-extrabold text-gray-900 dark:text-white tracking-tight sm:text-3xl">
              Architecture
            </h3>
            <p className="mt-3 text-lg text-gray-500 dark:text-gray-400">
              When you ask a peer a question, Repowire's daemon routes the query to the target session via tmux. It injects the query, waits for Claude to answer, and pipes the response back to you.
            </p>

            <dl className="mt-10 space-y-10">
              <div className="relative">
                <dt>
                  <div className="absolute flex items-center justify-center h-12 w-12 rounded-md bg-indigo-500 text-white font-bold text-xl">
                    1
                  </div>
                  <p className="ml-16 text-lg leading-6 font-medium text-gray-900 dark:text-white">Query Injection</p>
                </dt>
                <dd className="mt-2 ml-16 text-base text-gray-500 dark:text-gray-400">
                  You use the <code>ask_peer</code> tool. The daemon finds the target session in tmux and types the query for you.
                </dd>
              </div>

              <div className="relative">
                <dt>
                  <div className="absolute flex items-center justify-center h-12 w-12 rounded-md bg-indigo-500 text-white font-bold text-xl">
                    2
                  </div>
                  <p className="ml-16 text-lg leading-6 font-medium text-gray-900 dark:text-white">Context Retrieval</p>
                </dt>
                <dd className="mt-2 ml-16 text-base text-gray-500 dark:text-gray-400">
                  The peer Claude instance reads its local files, understands the current state, and formulates an answer.
                </dd>
              </div>

              <div className="relative">
                <dt>
                  <div className="absolute flex items-center justify-center h-12 w-12 rounded-md bg-indigo-500 text-white font-bold text-xl">
                    3
                  </div>
                  <p className="ml-16 text-lg leading-6 font-medium text-gray-900 dark:text-white">Response Delivery</p>
                </dt>
                <dd className="mt-2 ml-16 text-base text-gray-500 dark:text-gray-400">
                  Hooks capture the response and send it back to your original session instantly.
                </dd>
              </div>
            </dl>
          </div>

          <div className="mt-10 -mx-4 relative lg:mt-0" aria-hidden="true">
            <svg
              className="absolute left-1/2 transform -translate-x-1/2 translate-y-16 lg:hidden"
              width={784}
              height={404}
              fill="none"
              viewBox="0 0 784 404"
            >
              <defs>
                <pattern
                  id="ca9667ae-9f92-4be7-abcb-9e3d727f2941"
                  x={0}
                  y={0}
                  width={20}
                  height={20}
                  patternUnits="userSpaceOnUse"
                >
                  <rect x={0} y={0} width={4} height={4} className="text-gray-200 dark:text-gray-800" fill="currentColor" />
                </pattern>
              </defs>
              <rect width={784} height={404} fill="url(#ca9667ae-9f92-4be7-abcb-9e3d727f2941)" />
            </svg>
            <div className="relative mx-auto w-[90%] rounded-lg shadow-lg bg-gray-800 p-4 border border-gray-700">
               <div className="text-gray-400 font-mono text-xs mb-4 text-center">System Architecture</div>
               <div className="flex justify-between items-center space-x-4">
                  <div className="flex-1 bg-gray-900 p-4 rounded border border-gray-700 text-center">
                    <div className="text-blue-400 font-bold mb-2">Frontend</div>
                    <div className="text-xs text-gray-500">Claude Session</div>
                  </div>
                  <div className="flex-0 text-gray-500">
                    <svg className="w-6 h-6 animate-pulse text-blue-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M14 5l7 7m0 0l-7 7m7-7H3" /></svg>
                  </div>
                  <div className="flex-1 bg-indigo-900/50 p-4 rounded border border-indigo-500/50 text-center relative">
                    <div className="text-white font-bold mb-2">Daemon</div>
                    <div className="text-xs text-gray-400">Routing & Discovery</div>
                    <div className="absolute -top-3 -right-3 bg-green-500 text-white text-[10px] px-2 py-0.5 rounded-full">Active</div>
                  </div>
                  <div className="flex-0 text-gray-500">
                    <svg className="w-6 h-6 animate-pulse text-blue-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M14 5l7 7m0 0l-7 7m7-7H3" /></svg>
                  </div>
                  <div className="flex-1 bg-gray-900 p-4 rounded border border-gray-700 text-center">
                    <div className="text-purple-400 font-bold mb-2">Backend</div>
                    <div className="text-xs text-gray-500">Claude Session</div>
                  </div>
               </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
