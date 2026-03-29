import {
  BarElement,
  CategoryScale,
  Chart as ChartJS,
  Legend,
  LinearScale,
  Tooltip,
  type ChartData,
  type ChartOptions
} from "chart.js";
import ChartDataLabels from "chartjs-plugin-datalabels";
import { Bar } from "react-chartjs-2";

import type { MetricsAgentPoint } from "../../types";

ChartJS.register(CategoryScale, LinearScale, BarElement, Tooltip, Legend);

type BarChartAgentsProps = {
  agents: MetricsAgentPoint[];
};

export default function BarChartAgents({ agents }: BarChartAgentsProps) {
  const scoreOf = (row: MetricsAgentPoint) => Number(row.activity_score ?? row.decisions ?? 0);
  const sortedAgents = [...agents].sort((left, right) => scoreOf(right) - scoreOf(left));
  const labels = sortedAgents.map((row) => row.agent_id);
  const scores = sortedAgents.map((row) => scoreOf(row));

  const data: ChartData<"bar"> = {
    labels,
    datasets: [
      {
        label: "Activity score",
        data: scores,
        borderRadius: 6,
        backgroundColor: "rgba(47, 95, 160, 0.8)"
      }
    ]
  };

  const options: ChartOptions<"bar"> = {
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
        display: false
      },
      tooltip: {
        callbacks: {
          label: (context) => {
            const index = context.dataIndex;
            const point = sortedAgents[index];
            const activity = Number(context.parsed.y || 0).toFixed(0);
            const decisions = Number(point?.decisions || 0).toFixed(0);
            return `Activity: ${activity} • Final decisions: ${decisions}`;
          }
        }
      },
      datalabels: {
        display: (context) => Number(context.dataset.data[context.dataIndex] || 0) > 0,
        anchor: "end",
        align: "top",
        color: "#234f7f",
        formatter: (value: number) => Math.round(value).toString(),
        font: {
          weight: 600,
          size: 11
        }
      }
    },
    scales: {
      x: {
        offset: true,
        title: {
          display: true,
          text: "Agent ID"
        },
        ticks: {
          autoSkip: true,
          maxTicksLimit: 6,
          maxRotation: 20,
          minRotation: 20,
          padding: 4,
          callback: (_, index) => {
            const label = labels[index] ?? "";
            return label.length > 12 ? `${label.slice(0, 12)}...` : label;
          }
        }
      },
      y: {
        beginAtZero: true,
        title: {
          display: true,
          text: "Activity score"
        },
        ticks: {
          precision: 0
        }
      }
    }
  };

  return <Bar data={data} options={options} plugins={[ChartDataLabels]} />;
}
