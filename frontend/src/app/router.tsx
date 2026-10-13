import { createBrowserRouter } from 'react-router-dom'

import { DashboardPage } from '../pages/DashboardPage'
import { CallAssistantPage } from '../pages/CallAssistantPage'
import { KnowledgePage } from '../pages/KnowledgePage'
import { KnowledgeDocumentsPage } from '../pages/KnowledgeDocumentsPage'
import { ReviewPage } from '../pages/ReviewPage'
import { SettingsPage } from '../pages/SettingsPage'
import { ModelMonitoringPage } from '../pages/ModelMonitoringPage'
import { LoginPage } from '../pages/LoginPage'
import { ProtectedLayout } from '../auth/ProtectedLayout'

export const router = createBrowserRouter([
  { path: 'login', element: <LoginPage /> },
  {
    element: <ProtectedLayout />,
    children: [
      { index: true, element: <DashboardPage /> },
      { path: 'calls', element: <CallAssistantPage /> },
      { path: 'knowledge', element: <KnowledgePage /> },
      { path: 'knowledge/documents', element: <KnowledgeDocumentsPage /> },
      { path: 'reviews', element: <ReviewPage /> },
      { path: 'monitoring', element: <ModelMonitoringPage /> },
      { path: 'settings', element: <SettingsPage /> },
    ],
  },
])
