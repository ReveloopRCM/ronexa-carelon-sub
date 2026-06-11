import { badgeClass } from "../utils";

export default function StatusBadge({ value }: { value: string }) {
  return (
    <span className={`px-2 py-0.5 rounded-full text-xs ${badgeClass(value)}`}>
      {value || "-"}
    </span>
  );
}