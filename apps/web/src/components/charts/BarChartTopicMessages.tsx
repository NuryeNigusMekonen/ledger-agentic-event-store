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
import { Bar } from "react-chartjs-2";

import type { DeliveryHealth } from "../../types";

ChartJS.register(CategoryScale, LinearScale, BarElement, Tooltip, Legend);

type BarChartTopicMessagesProps = {
  topics: DeliveryHealth["topics"];
};

export default function BarChartTopicMessages({ topics }: BarChartTopicMessagesProps) {
  const sortedTopics = [...topics].sort((left, right) => right.published_last_1h - left.published_last_1h);
  const labels = sortedTopics.map((topic) => topic.topic);

  const data: ChartData<"bar"> = {
    labels,
    datasets: [
      {
        label: "Published (1h)",
        data: sortedTopics.map((topic) => topic.published_last_1h),
        borderRadius: 6,
        backgroundColor: "rgba(47, 137, 105, 0.76)"
      }
    ]
  };

  const options: ChartOptions<"bar"> = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: {
        display: false
      },
      tooltip: {
        callbacks: {
          title: (context) => labels[context[0]?.dataIndex ?? 0] ?? "",
          label: (context) => {
            const index = context.dataIndex;
            const topic = sortedTopics[index];
            if (!topic) {
              return "Published: 0";
            }
            return `Published: ${topic.published_last_1h} | Pending: ${topic.pending} | DLQ: ${topic.dead_letter}`;
          }
        }
      }
    },
    scales: {
      x: {
        title: {
          display: true,
          text: "Topic"
        },
        ticks: {
          autoSkip: false,
          maxRotation: 25,
          minRotation: 12,
          callback: (_, index) => {
            const label = labels[index] ?? "";
            return label.length > 22 ? `${label.slice(0, 22)}...` : label;
          }
        }
      },
      y: {
        beginAtZero: true,
        title: {
          display: true,
          text: "Messages (1h)"
        },
        ticks: {
          precision: 0
        }
      }
    }
  };

  return <Bar data={data} options={options} />;
}
