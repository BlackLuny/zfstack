import unittest

from cubic50_env import parse_stat


class ProcObservationTests(unittest.TestCase):
    def test_stat_comm_with_spaces_and_parentheses_keeps_linux_field_numbers(self):
        # Fields 3..52, with selected values at their Linux-defined positions.
        fields=['0']*50
        for index,value in [(0,'S'),(1,'41'),(11,'123'),(12,'456'),(19,'987654'),(36,'3')]:fields[index]=value
        result=parse_stat('42 (worker (CPU) copy) '+' '.join(fields))
        self.assertEqual(result,dict(ppid=41,user_ticks=123,system_ticks=456,start_ticks=987654,last_cpu=3))

if __name__=='__main__':unittest.main()
