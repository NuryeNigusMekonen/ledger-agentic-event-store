import {
  CategoryScale,
  Chart as ChartJS,
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
import { formatDuration } from "../../utils/formatters";

ChartJS.register(CategoryScale, LinearScale, PointElement, LineElement, Tooltip, Legend);

type LineChartProcessingProps = {
  points: MetricsDailyPoint[];
};

export default function LineChartProcessing({ points }: LineChartProcessingProps) {
  const labels = points.map((point) =>
    new Date(`${point.date}T00:00:00Z`).toLocaleDateString(undefined, { month: "short", day: "numeric" })
  );
  const pointCount = points.length;
  const isDenseWindow = pointCount > 20;
  const maxTicksLimit = pointCount > 60 ? 5 : pointCount > 35 ? 6 : 8;
  const tickRotation = isDenseWindow ? 0 : 20;
  const maxSeconds = Math.max(...points.map((point) => point.avg_processing_seconds), 0);
  const showSeconds = maxSeconds > 0 && maxSeconds < 60;
  const plottedValues = points.map((point) =>
    showSeconds ? point.avg_processing_seconds : point.avg_processing_seconds / 60
  );

  const data: ChartData<"line"> = {
    labels,
    datasets: [
      {
        label: showSeconds ? "Avg processing time (sec)" : "Avg processing time (min)",
        data: plottedValues,
        borderColor: "#97743f",
        backgroundColor: "rgba(151, 116, 63, 0.16)",
        tension: 0.18,
        pointRadius: 3,
        spanGaps: true
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
        callbacks: {
          label: (context) => {
            const index = context.dataIndex;
            const secondsValue = points[index]?.avg_processing_seconds ?? 0;
            return `Average: ${formatDuration(secondsValue)}`;
          }
        }
      }
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
          text: showSeconds ? "Processing time (sec)" : "Processing time (min)"
        },
        ticks: {
          callback: (value) => {
            const numeric = typeof value === "number" ? value : Number(value);
            if (!Number.isFinite(numeric)) {
              return "0";
            }
            return numeric.toFixed(2).replace(/\.00$/, "");
          }
        }
      }
    }
  };

  return <Line data={data} options={options} />;
}
