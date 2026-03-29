import {
  CategoryScale,
  Chart as ChartJS,
  Filler,
  Legend,
  LineElement,
  LinearScale,
  PointElement,
  Tooltip,
  type ChartData,
  type ChartOptions
} from "chart.js";
import { Line } from "react-chartjs-2";

import type { MetricsDailyPoint } from "../../types";

ChartJS.register(CategoryScale, LinearScale, PointElement, LineElement, Filler, Tooltip, Legend);

type LineChartDailyProps = {
  points: MetricsDailyPoint[];
};

export default function LineChartDaily({ points }: LineChartDailyProps) {
  const labels = points.map((point) =>
    new Date(`${point.date}T00:00:00Z`).toLocaleDateString(undefined, { month: "short", day: "numeric" })
  );
  const pointCount = points.length;
  const isDenseWindow = pointCount > 20;
  const maxTicksLimit = pointCount > 60 ? 5 : pointCount > 35 ? 6 : 8;
  const tickRotation = isDenseWindow ? 0 : 20;

  const data: ChartData<"line"> = {
    labels,
    datasets: [
      {
        label: "Submitted",
        data: points.map((point) => point.submitted),
        borderColor: "#2f5fa0",
        backgroundColor: "rgba(47, 95, 160, 0.14)",
        fill: true,
        tension: 0.18,
        pointRadius: 3
      },
      {
        label: "Approved",
        data: points.map((point) => point.approved),
        borderColor: "#2f8969",
        backgroundColor: "rgba(47, 137, 105, 0.12)",
        fill: true,
        tension: 0.18,
        pointRadius: 3
      }
    ]
  };

  const options: ChartOptions<"line"> = {
    responsive: true,
    maintainAspectRatio: false,
    layout: {
      padding: {
        left: 6,
        right: isDenseWindow ? 24 : 18,
        top: 4,
        bottom: 6
      }
    },
    plugins: {
      legend: {
        position: "top"
      },
      datalabels: {
        display: false
      },
      tooltip: {
        mode: "index",
        intersect: false,
        callbacks: {
          label: (context) => `${context.dataset.label}: ${Number(context.parsed.y || 0).toFixed(0)}`
        }
      }
    },
    interaction: {
      mode: "index",
      intersect: false
    },
    scales: {
      x: {
        offset: true,
        title: {
          display: true,
          text: "Date"
        },
        ticks: {
          autoSkip: true,
          autoSkipPadding: 12,
          maxTicksLimit,
          maxRotation: tickRotation,
          minRotation: tickRotation,
          padding: 4
        }
      },
      y: {
        beginAtZero: true,
        title: {
          display: true,
          text: "Applications"
        },
        ticks: {
          precision: 0
        }
      }
    }
  };

  return <Line data={data} options={options} />;
}
