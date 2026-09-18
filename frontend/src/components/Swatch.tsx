// A colour shown as a swatch + name; the hex lives only in the tooltip (never as bare text).
import styles from "./Swatch.module.css";

export function Swatch({ name, hex }: { name: string; hex: string }) {
  return (
    <span className={styles.swatch} title={hex} data-hex={hex}>
      <span className={styles.chip} style={{ backgroundColor: hex }} aria-hidden="true" />
      {name}
    </span>
  );
}
