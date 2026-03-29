import {
  ArcElement,
  Chart as ChartJS,
  Legend,
  Tooltip,
  type ChartData,
  type ChartOptions
} from "chart.js";
import { Pie } from "react-chartjs-2";

ChartJS.register(ArcElement, Tooltip, Legend);

type PieChartApprovalProps = {
  approved: number;
  declined: number;
};

export default function PieChartApproval({ approved, declined }: PieChartApprovalProps) {
  const total = approved + declined;

  const data: ChartData<"pie"> = {
    labels: ["Approved", "Declined"],
    datasets: [
      {
        data: [approved, declined],
        backgroundColor: ["#2f8969", "#b04a48"],
        borderColor: ["#2f8969", "#b04a48"],
        borderWidth: 1
      }
    ]
  };

  const options: ChartOptions<"pie"> = {
    responsive: true,
    maintainAspectRatio: false,
    layout: {
      padding: {
        left: 6,
        right: 18,
        top: 4,
        bottom: 6
      }
    },
    plugins: {
      legend: {
        position: "bottom",
        labels: {
          padding: 14,
          boxWidth: 14,
          boxHeight: 14,
          generateLabels: (chart) => {
            const labels = chart.data.labels ?? [];
            const dataset = chart.data.datasets[0];
            const values = (dataset?.data ?? []) as Array<number | null>;
            const sum = values.reduce<number>((acc, value) => acc + Number(value || 0), 0);

            return labels.map((label, index) => {
              const raw = Number(values[index] || 0);
              const pct = sum > 0 ? (raw / sum) * 100 : 0;
              return {
                text: `${String(label)} ${pct.toFixed(1)}%`,
                fillStyle: Array.isArray(dataset?.backgroundColor)
                  ? (dataset?.backgroundColor[index] as string)
                  : "#2f5fa0",
                strokeStyle: Array.isArray(dataset?.borderColor)
                  ? (dataset?.borderColor[index] as string)
                  : "#2f5fa0",
                lineWidth: 1,
                hidden: false,
                index,
              };
            });
          }
        }
      },
      datalabels: {
        display: false
      },
      tooltip: {
        callbacks: {
          label: (context) => {
            const value = Number(context.parsed || 0);
            const pct = total > 0 ? (value / total) * 100 : 0;
            return `${context.label}: ${value} (${pct.toFixed(1)}%)`;
          }
        }
      }
    }
  };

  return <Pie data={data} options={options} />;
}
