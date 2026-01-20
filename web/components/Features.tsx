import { Share2, Zap, Server, Shield, Network, RefreshCw } from 'lucide-react';

const features = [
  {
    name: 'Sync Communication',
    description: 'Real-time messaging between active coding sessions. No stale context or manual updates.',
    icon: Zap,
  },
  {
    name: 'Multi-Repo Context',
    description: 'Ask questions about code in other repositories without leaving your current session.',
    icon: Share2,
  },
  {
    name: 'Tmux Integration',
    description: 'Seamlessly integrates with tmux to manage and discover active Claude sessions.',
    icon: Server,
  },
  {
    name: 'Daemon Architecture',
    description: 'Central daemon manages peer discovery and routing, running as a robust system service.',
    icon: Network,
  },
  {
    name: 'Secure & Local',
    description: 'All communication happens locally on your machine by default. No data leaves your network.',
    icon: Shield,
  },
  {
    name: 'Auto-Discovery',
    description: 'Sessions automatically find each other. Just start Claude in a repo and it joins the mesh.',
    icon: RefreshCw,
  },
];

export default function Features() {
  return (
    <div className="py-24 bg-gray-50 dark:bg-gray-900 transition-colors">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
        <div className="lg:text-center">
          <h2 className="text-base text-blue-600 font-semibold tracking-wide uppercase">Features</h2>
          <p className="mt-2 text-3xl leading-8 font-extrabold tracking-tight text-gray-900 dark:text-white sm:text-4xl">
            A better way to manage context
          </p>
          <p className="mt-4 max-w-2xl text-xl text-gray-500 dark:text-gray-400 lg:mx-auto">
            Repowire bridges the gap between isolated AI sessions, creating a collaborative mesh of intelligence for your entire codebase.
          </p>
        </div>

        <div className="mt-20">
          <dl className="space-y-10 md:space-y-0 md:grid md:grid-cols-2 md:gap-x-8 md:gap-y-10">
            {features.map((feature) => (
              <div key={feature.name} className="relative">
                <dt>
                  <div className="absolute flex items-center justify-center h-12 w-12 rounded-md bg-blue-500 text-white">
                    <feature.icon className="h-6 w-6" aria-hidden="true" />
                  </div>
                  <p className="ml-16 text-lg leading-6 font-medium text-gray-900 dark:text-white">{feature.name}</p>
                </dt>
                <dd className="mt-2 ml-16 text-base text-gray-500 dark:text-gray-400">
                  {feature.description}
                </dd>
              </div>
            ))}
          </dl>
        </div>
      </div>
    </div>
  );
}
