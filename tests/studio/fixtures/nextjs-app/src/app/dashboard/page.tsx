import { Usage } from "@/components/dashboard/Usage";
import { useState } from "react";
export default function Dashboard() { const [range] = useState("30d"); return <Usage range={range} />; }
