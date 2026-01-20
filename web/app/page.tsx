import Navbar from '@/components/Navbar';
import Hero from '@/components/Hero';
import Features from '@/components/Features';
import HowItWorks from '@/components/HowItWorks';
import Installation from '@/components/Installation';
import Footer from '@/components/Footer';

export default function Home() {
  return (
    <main className="min-h-screen bg-white dark:bg-black selection:bg-blue-100 dark:selection:bg-blue-900">
      <Navbar />
      <Hero />
      <Features />
      <HowItWorks />
      <Installation />
      <Footer />
    </main>
  );
}