import Link from 'next/link';
export default function Home() {
  return (
    <ul>
      <li><Link href='/configs'>Configs</Link></li>
      <li><Link href='/run'>Run</Link></li>
      <li><Link href='/runs'>Runs</Link></li>
      <li><Link href='/grid'>Grid</Link></li>
      <li><Link href='/live_result'>Live Result</Link></li>
    </ul>
  );
}
